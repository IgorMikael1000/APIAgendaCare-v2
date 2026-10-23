import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Any, Optional
from urllib.parse import quote

import firebase_admin
import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from google.oauth2 import service_account
import requests
from firebase_admin import auth, credentials, exceptions
from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Path,
    Query,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from database import criar_indices_performance, get_db
import models
import schemas
from schemas import AppointmentSchema, EvolutionRecordSchema

bearer_scheme = HTTPBearer(auto_error=False)
firebase_app_lock = Lock()
logger = logging.getLogger(__name__)
startup_tasks: set[asyncio.Task[None]] = set()

GOOGLE_PLAY_SERVICE_ACCOUNT_ENV = "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"
GOOGLE_PLAY_PACKAGE_ENV = "GOOGLE_PLAY_PACKAGE_NAME"
GOOGLE_PLAY_PUBSUB_AUDIENCE_ENV = "GOOGLE_PLAY_PUBSUB_AUDIENCE"
GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_ENV = (
    "GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_EMAIL"
)
FIREBASE_WEB_API_KEY_ENV = "FIREBASE_WEB_API_KEY"

GOOGLE_PLAY_PRODUCT_PLANS = {
    "plan_monthly": "MONTHLY",
    "plan_quarterly": "QUARTERLY",
    "plan_semiannual": "SEMIANNUAL",
    "plan_annual": "ANNUAL",
}
GOOGLE_PLAY_SUBSCRIPTION_STATUSES = {
    "SUBSCRIPTION_STATE_ACTIVE": "ACTIVE",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD": "ACTIVE",
    "SUBSCRIPTION_STATE_CANCELED": "CANCELED",
    "SUBSCRIPTION_STATE_ON_HOLD": "ON_HOLD",
    "SUBSCRIPTION_STATE_PAUSED": "PAUSED",
    "SUBSCRIPTION_STATE_EXPIRED": "EXPIRED",
    "SUBSCRIPTION_STATE_PENDING": "PENDING",
    "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED": "EXPIRED",
}
ACCESSIBLE_SUBSCRIPTION_STATUSES = {"ACTIVE", "CANCELED"}

async def _run_performance_index_setup():
    try:
        await asyncio.to_thread(criar_indices_performance)
    except Exception:
        logger.exception(
            "Falha inesperada na verificação dos índices; "
            "a API continuará disponível"
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_run_performance_index_setup())
    startup_tasks.add(task)
    task.add_done_callback(startup_tasks.discard)
    yield


app = FastAPI(
    title="AgendaCare API",
    version="1.0.0",
    lifespan=lifespan,
)


def _require_timezone_aware_updated_since(updated_since: Optional[datetime]):
    if (
        updated_since is not None
        and (
            updated_since.tzinfo is None
            or updated_since.utcoffset() is None
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="updated_since deve incluir fuso horário (ISO 8601)",
        )


@app.get(
    "/api/v1/config/version",
    response_model=schemas.AppSettingsResponse,
)
def get_app_version_config(db: Session = Depends(get_db)):
    settings = (
        db.query(models.AppSettingsModel)
        .filter(models.AppSettingsModel.id == 1)
        .first()
    )
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Configuração de versão do aplicativo indisponível",
        )

    return {
        "latest_version_code": settings.latest_version_code,
        "force_update": settings.force_update,
    }


def get_firebase_app():
    try:
        return firebase_admin.get_app()
    except ValueError:
        with firebase_app_lock:
            try:
                return firebase_admin.get_app()
            except ValueError:
                firebase_cred_env = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
                if firebase_cred_env:
                    cred_dict = json.loads(firebase_cred_env)
                    cred = credentials.Certificate(cred_dict)
                    return firebase_admin.initialize_app(cred)
                return firebase_admin.initialize_app()


def verify_firebase_token(
    bearer_credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
) -> dict[str, Any]:
    if bearer_credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticação obrigatória",
            headers={"WWW-Authenticate": "Bearer"},
        )

    firebase_app = get_firebase_app()
    try:
        decoded_token = auth.verify_id_token(
            bearer_credentials.credentials,
            app=firebase_app,
            check_revoked=True,
        )
    except (auth.InvalidIdTokenError, auth.UserDisabledError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token Firebase inválido ou expirado",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except exceptions.FirebaseError as exc:
        logger.exception("Não foi possível validar o token no Firebase")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Não foi possível validar o token no Firebase",
        ) from exc

    firebase_uid = decoded_token.get("uid")
    if not firebase_uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token Firebase inválido ou expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return decoded_token


def get_current_user(
    firebase_token: dict[str, Any] = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
) -> models.ProfessionalModel:
    current_user = (
        db.query(models.ProfessionalModel)
        .filter(
            models.ProfessionalModel.firebase_uid == firebase_token["uid"]
        )
        .first()
    )
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Não existe um profissional vinculado a este usuário Firebase",
        )
    return current_user


def professional_response(prof: models.ProfessionalModel):
    if not prof.cpf:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Profissional sem CPF cadastrado; atualize os dados cadastrais",
        )

    expiration = subscription_expiration(prof)
    subscription_expired = is_subscription_expired(prof)
    return {
        "id": prof.id,
        "name": prof.name,
        "email": prof.email,
        "specialty": prof.specialty,
        "specialtyId": prof.specialty_id,
        "professionalRegister": prof.professional_register,
        "phone": prof.phone,
        "cpf": prof.cpf,
        "planType": prof.plan_type,
        "subscriptionStatus": prof.subscription_status,
        "trialEndsAt": format_expiration_timestamp(prof.trial_ends_at),
        "subscriptionExpiresAt": format_expiration_timestamp(
            prof.subscription_expires_at
        ),
        "expirationDate": format_expiration_date(expiration),
        "isSubscriptionExpired": subscription_expired,
        "bloquear_app": subscription_expired,
    }


def format_expiration_date(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _as_utc(value).date().isoformat()


def format_expiration_timestamp(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def subscription_expiration(
    prof: models.ProfessionalModel,
) -> Optional[datetime]:
    subscription_status = (prof.subscription_status or "").upper()
    if subscription_status == "TRIAL":
        return prof.trial_ends_at
    if subscription_status in {
        "ACTIVE",
        "CANCELED",
        "IN_GRACE_PERIOD",
        "ON_HOLD",
        "PAUSED",
        "PENDING",
        "EXPIRED",
    }:
        return prof.subscription_expires_at
    return None


def is_subscription_expired(
    prof: models.ProfessionalModel,
    now: Optional[datetime] = None,
) -> bool:
    subscription_status = (prof.subscription_status or "").upper()
    if subscription_status == "TRIAL":
        expiration = prof.trial_ends_at
    elif subscription_status in ACCESSIBLE_SUBSCRIPTION_STATUSES:
        expiration = prof.subscription_expires_at
    else:
        return True

    if expiration is None:
        return True

    current_time = _as_utc(now or datetime.now(timezone.utc))
    return _as_utc(expiration) <= current_time


def google_play_subscription_snapshot(
    subscription: schemas.GooglePlaySubscriptionDetails,
) -> tuple[str, str, Optional[datetime]]:
    try:
        subscription_status = GOOGLE_PLAY_SUBSCRIPTION_STATUSES[
            subscription.subscriptionState
        ]
    except KeyError as exc:
        logger.error(
            "Estado de assinatura Google Play não suportado: %s",
            subscription.subscriptionState,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Estado de assinatura da Google Play não reconhecido",
        ) from exc

    if len(subscription.lineItems) != 1:
        logger.error(
            "Google Play retornou uma assinatura com quantidade inesperada "
            "de itens de linha: %s",
            len(subscription.lineItems),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="A resposta da Google Play contém itens de plano ambíguos",
        )

    line_item = subscription.lineItems[0]
    plan_type = GOOGLE_PLAY_PRODUCT_PLANS.get(line_item.productId)
    if plan_type is None:
        logger.error("Produto de assinatura Google Play não reconhecido")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="A Google Play retornou um produto de assinatura não reconhecido",
        )

    expiration = (
        _as_utc(line_item.expiryTime)
        if line_item.expiryTime is not None
        else None
    )
    if subscription_status != "PENDING" and expiration is None:
        logger.error(
            "Google Play retornou estado %s sem data de expiração",
            subscription.subscriptionState,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="A resposta da Google Play não contém uma expiração válida",
        )

    return subscription_status, plan_type, expiration


def _subscription_token_owners(
    db: Session,
    purchase_tokens: set[str],
) -> set[str]:
    if not purchase_tokens:
        return set()

    registered_owners = {
        row.professional_id
        for row in db.query(models.SubscriptionPurchaseTokenModel)
        .filter(
            models.SubscriptionPurchaseTokenModel.purchase_token.in_(
                purchase_tokens
            )
        )
        .all()
    }
    legacy_owners = {
        row.id
        for row in db.query(models.ProfessionalModel)
        .filter(
            models.ProfessionalModel.purchase_token.in_(purchase_tokens)
        )
        .all()
    }
    return registered_owners | legacy_owners


def _external_account_id(
    subscription: schemas.GooglePlaySubscriptionDetails,
) -> Optional[str]:
    if subscription.externalAccountIdentifiers is None:
        return None
    return (
        subscription.externalAccountIdentifiers.obfuscatedExternalAccountId
    )


def _professional_subscription_tokens(
    db: Session,
    professional: models.ProfessionalModel,
) -> set[str]:
    tokens = set(
        db.query(models.SubscriptionPurchaseTokenModel.purchase_token)
        .filter(
            models.SubscriptionPurchaseTokenModel.professional_id
            == professional.id
        )
        .all()
    )
    token_values = {row[0] for row in tokens}
    if professional.purchase_token:
        token_values.add(professional.purchase_token)
    return token_values


def _resolve_subscription_professional(
    db: Session,
    subscription: schemas.GooglePlaySubscriptionDetails,
    purchase_tokens: set[str],
) -> models.ProfessionalModel:
    account_id = _external_account_id(subscription)
    account_professional = None
    if account_id:
        account_professional = (
            db.query(models.ProfessionalModel)
            .filter(models.ProfessionalModel.firebase_uid == account_id)
            .first()
        )

    token_owners = _subscription_token_owners(db, purchase_tokens)
    if len(token_owners) > 1:
        logger.error(
            "Tokens vinculados a uma notificação pertencem a contas "
            "diferentes"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Os tokens da assinatura estão associados a contas diferentes",
        )

    token_owner_id = next(iter(token_owners), None)
    if (
        account_professional is not None
        and token_owner_id is not None
        and account_professional.id != token_owner_id
    ):
        logger.error(
            "Identificador de conta Google Play não corresponde ao dono "
            "registrado do token"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A assinatura Google Play está associada a outra conta",
        )

    professional_id = (
        account_professional.id
        if account_professional is not None
        else token_owner_id
    )
    if professional_id is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A assinatura ainda não pôde ser associada a uma conta",
            headers={"Retry-After": "60"},
        )

    professional = (
        db.query(models.ProfessionalModel)
        .filter(models.ProfessionalModel.id == professional_id)
        .with_for_update()
        .first()
    )
    if professional is None:
        logger.warning(
            "A conta associada à assinatura Google Play não existe mais"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A conta associada à assinatura ainda não está disponível",
            headers={"Retry-After": "60"},
        )

    if account_id and professional.firebase_uid != account_id:
        logger.error(
            "Identificador de conta Google Play não corresponde ao "
            "profissional resolvido"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A assinatura Google Play está associada a outra conta",
        )
    return professional


def _record_subscription_tokens(
    db: Session,
    professional: models.ProfessionalModel,
    purchase_tokens: set[str],
) -> None:
    for purchase_token in purchase_tokens:
        existing = (
            db.query(models.SubscriptionPurchaseTokenModel)
            .filter(
                models.SubscriptionPurchaseTokenModel.purchase_token
                == purchase_token
            )
            .first()
        )
        if existing is not None:
            if existing.professional_id != professional.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="O token da assinatura já está associado a outra conta",
                )
            continue

        db.add(
            models.SubscriptionPurchaseTokenModel(
                purchase_token=purchase_token,
                professional_id=professional.id,
            )
        )


def _google_play_event_key(message: schemas.GooglePubSubMessage) -> str:
    event_identity = (
        f"message:{message.messageId}"
        if message.messageId
        else f"data:{message.data}"
    )
    return hashlib.sha256(event_identity.encode("utf-8")).hexdigest()


def _purchase_token_hash(purchase_token: str) -> str:
    return hashlib.sha256(purchase_token.encode("utf-8")).hexdigest()


def _persist_pending_google_play_event(
    db: Session,
    event_key: str,
    purchase_token: str,
    notification_type: int,
    event_time_millis: Optional[str],
) -> bool:
    existing = (
        db.query(models.GooglePlayWebhookEventModel)
        .filter_by(event_key=event_key)
        .first()
    )
    if existing is not None:
        return False

    db.add(
        models.GooglePlayWebhookEventModel(
            event_key=event_key,
            purchase_token_hash=_purchase_token_hash(purchase_token),
            notification_type=notification_type,
            event_time_millis=event_time_millis,
            status="PENDING",
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if (
            db.query(models.GooglePlayWebhookEventModel)
            .filter_by(event_key=event_key)
            .first()
            is not None
        ):
            return False
        raise
    except SQLAlchemyError:
        db.rollback()
        raise
    return True


def _mark_pending_google_play_events_processed(
    db: Session,
    purchase_tokens: set[str],
    processed_at: datetime,
) -> int:
    if not purchase_tokens:
        return 0

    token_hashes = {
        _purchase_token_hash(purchase_token)
        for purchase_token in purchase_tokens
    }
    events = (
        db.query(models.GooglePlayWebhookEventModel)
        .filter(
            models.GooglePlayWebhookEventModel.purchase_token_hash.in_(
                token_hashes
            ),
            models.GooglePlayWebhookEventModel.status == "PENDING",
        )
        .with_for_update()
        .all()
    )
    for event in events:
        event.status = "PROCESSED"
        event.purchase_token_hash = None
        event.processed_at = processed_at
        event.attempt_count += 1
    return len(events)


def _mark_google_play_event_processed(
    db: Session,
    event_key: str,
    notification: schemas.GooglePlaySubscriptionNotification,
    event_time_millis: Optional[str],
) -> None:
    event = (
        db.query(models.GooglePlayWebhookEventModel)
        .filter_by(event_key=event_key)
        .with_for_update()
        .first()
    )
    if event is None:
        event = models.GooglePlayWebhookEventModel(
            event_key=event_key,
            notification_type=notification.notificationType,
            event_time_millis=event_time_millis,
            status="PROCESSED",
            processed_at=datetime.now(timezone.utc),
            attempt_count=1,
        )
        db.add(event)
    else:
        event.purchase_token_hash = None
        event.notification_type = notification.notificationType
        event.event_time_millis = event_time_millis
        event.status = "PROCESSED"
        event.processed_at = datetime.now(timezone.utc)
        event.attempt_count += 1


def _record_rejected_google_play_event(
    db: Session,
    event_key: str,
    notification: schemas.GooglePlaySubscriptionNotification,
    event_time_millis: Optional[str],
    reason: str,
) -> dict[str, str]:
    event = (
        db.query(models.GooglePlayWebhookEventModel)
        .filter_by(event_key=event_key)
        .with_for_update()
        .first()
    )
    if event is not None and event.status in {"PROCESSED", "REJECTED"}:
        db.rollback()
        return {"status": "acknowledged", "result": "duplicate_event"}
    if event is None:
        event = models.GooglePlayWebhookEventModel(
            event_key=event_key,
            notification_type=notification.notificationType,
            event_time_millis=event_time_millis,
            status="REJECTED",
            processed_at=datetime.now(timezone.utc),
            attempt_count=1,
        )
        db.add(event)
    else:
        event.purchase_token_hash = None
        event.notification_type = notification.notificationType
        event.event_time_millis = event_time_millis
        event.status = "REJECTED"
        event.processed_at = datetime.now(timezone.utc)
        event.attempt_count += 1

    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Não foi possível registrar evento Google Play rejeitado: "
            "event_id=%s erro=%s",
            event_key,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível registrar a notificação Google Play",
        ) from exc

    logger.warning(
        "Evento Google Play não aplicado: event_id=%s motivo=%s",
        event_key,
        reason,
    )
    return {"status": "acknowledged", "result": "rejected_event"}


def _reconcile_pending_events_after_verification(
    db: Session,
    professional: models.ProfessionalModel,
    package_name: str,
) -> int:
    current_token = professional.purchase_token
    if not current_token:
        return 0

    current_tokens = _professional_subscription_tokens(db, professional)
    pending_hashes = {
        _purchase_token_hash(token) for token in current_tokens
    }
    pending_count = (
        db.query(models.GooglePlayWebhookEventModel)
        .filter(
            models.GooglePlayWebhookEventModel.purchase_token_hash.in_(
                pending_hashes
            ),
            models.GooglePlayWebhookEventModel.status == "PENDING",
        )
        .count()
    )
    if not pending_count:
        return 0

    try:
        subscription = fetch_google_play_subscription(
            current_token,
            package_name,
        )
    except HTTPException as exc:
        logger.warning(
            "Reconciliação RTDN pendente aguardará nova tentativa: "
            "status=%s",
            exc.status_code,
        )
        raise

    subscription_status, plan_type, expiration = (
        google_play_subscription_snapshot(subscription)
    )
    if subscription_status == "PENDING":
        return 0
    if (
        subscription_status == "ACTIVE"
        and expiration is not None
        and expiration <= datetime.now(timezone.utc)
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="A Google Play retornou uma assinatura ativa já expirada",
        )

    account_id = _external_account_id(subscription)
    if account_id and account_id != professional.firebase_uid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A assinatura Google Play está associada a outra conta",
        )

    linked_token = subscription.linkedPurchaseToken
    if linked_token:
        current_tokens.add(linked_token)
    token_owners = _subscription_token_owners(db, current_tokens)
    if token_owners and token_owners != {professional.id}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A assinatura está associada a outra conta",
        )
    if not account_id and token_owners != {professional.id}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Não foi possível confirmar o vínculo da compra à conta",
        )

    _record_subscription_tokens(db, professional, current_tokens)
    professional.plan_type = plan_type
    professional.subscription_status = subscription_status
    professional.subscription_expires_at = expiration
    reconciled_count = _mark_pending_google_play_events_processed(
        db,
        current_tokens,
        datetime.now(timezone.utc),
    )
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Não foi possível persistir reconciliação RTDN: "
            "erro=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível atualizar a assinatura",
        ) from exc
    db.refresh(professional)
    logger.info(
        "Eventos RTDN reconciliados após vínculo: eventos=%s",
        reconciled_count,
    )
    return reconciled_count


def verify_google_play_pubsub_token(
    bearer_credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
) -> None:
    audience = os.getenv(GOOGLE_PLAY_PUBSUB_AUDIENCE_ENV)
    service_account_email = os.getenv(
        GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_ENV
    )
    if not audience or not service_account_email:
        logger.error(
            "Autenticação Pub/Sub indisponível: configure %s e %s",
            GOOGLE_PLAY_PUBSUB_AUDIENCE_ENV,
            GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_ENV,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Autenticação do webhook Google Play não configurada",
        )
    if bearer_credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticação Pub/Sub obrigatória",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = google_id_token.verify_oauth2_token(
            bearer_credentials.credentials,
            GoogleAuthRequest(),
            audience=audience,
        )
    except google.auth.exceptions.TransportError as exc:
        logger.error(
            "Não foi possível validar o token OIDC do Pub/Sub (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Não foi possível validar a autenticação do webhook",
            headers={"Retry-After": "60"},
        ) from exc
    except (ValueError, google.auth.exceptions.GoogleAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token OIDC do Pub/Sub inválido",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if claims.get("iss") not in {
        "accounts.google.com",
        "https://accounts.google.com",
    }:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Emissor do token Pub/Sub inválido",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if (
        claims.get("email") != service_account_email
        or claims.get("email_verified") is not True
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Identidade do push Pub/Sub não autorizada",
        )


def require_active_professional(
    professional_id: str = Path(...),
    current_user: models.ProfessionalModel = Depends(get_current_user),
) -> models.ProfessionalModel:
    if current_user.id != professional_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso não autorizado a este profissional",
        )
    if is_subscription_expired(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Assinatura expirada",
        )
    return current_user


@app.get("/")
def read_root():
    return {"status": "online", "app": "AgendaCare API"}

@app.post(
    "/api/v1/auth/register",
    response_model=schemas.ProfessionalResponse,
)
def register(
    prof: schemas.ProfessionalCreate,
    firebase_token: dict[str, Any] = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    firebase_uid = firebase_token["uid"]
    firebase_email = firebase_token.get("email")
    if not firebase_email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="O token Firebase não contém um e-mail verificado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    firebase_email = firebase_email.strip().casefold()
    if prof.email.strip().casefold() != firebase_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="O e-mail informado não corresponde ao usuário autenticado",
        )

    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:android_id))"),
            {"android_id": prof.android_id},
        )

    duplicate_credentials = [
        func.lower(models.ProfessionalModel.email) == firebase_email,
    ]
    cpf = prof.cpf.strip()
    duplicate_credentials.append(models.ProfessionalModel.cpf == cpf)
    if prof.phone is not None:
        duplicate_credentials.append(models.ProfessionalModel.phone == prof.phone)

    existing = (
        db.query(models.ProfessionalModel)
        .filter(or_(*duplicate_credentials))
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="E-mail, CPF ou telefone já cadastrados no sistema",
        )
    if db.query(models.ProfessionalModel.id).filter(
        models.ProfessionalModel.id == prof.id
    ).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ID de profissional já cadastrado no sistema",
        )

    existing_device = (
        db.query(models.TrialDeviceModel)
        .filter(models.TrialDeviceModel.android_id == prof.android_id)
        .first()
    )
    device_was_existing = existing_device is not None
    current_date = datetime.now(timezone.utc)
    if existing_device:
        trial_ends_at = _as_utc(existing_device.data_ativacao) + timedelta(
            days=7
        )
    else:
        activation_date = current_date
        trial_ends_at = activation_date + timedelta(days=7)
        db.add(
            models.TrialDeviceModel(
                android_id=prof.android_id,
                data_ativacao=activation_date,
            )
        )

    db_prof = models.ProfessionalModel(
        id=prof.id,
        firebase_uid=firebase_uid,
        plan_type="FREE",
        trial_ends_at=trial_ends_at,
        subscription_status=(
            "TRIAL" if current_date < trial_ends_at else "EXPIRED"
        ),
        name=prof.name,
        email=firebase_email,
        specialty=prof.specialty,
        specialty_id=prof.specialtyId,
        professional_register=prof.professionalRegister,
        cpf=cpf,
        phone=prof.phone,
        signature_url=prof.signatureUrl
    )
    db.add(db_prof)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not existing_device:
            existing_device = (
                db.query(models.TrialDeviceModel)
                .filter(
                    models.TrialDeviceModel.android_id == prof.android_id
                )
                .first()
            )
        if existing_device and not device_was_existing:
            activation_date = _as_utc(existing_device.data_ativacao)
            trial_ends_at = activation_date + timedelta(days=7)
            db_prof.trial_ends_at = trial_ends_at
            db_prof.subscription_status = (
                "TRIAL"
                if datetime.now(timezone.utc) < trial_ends_at
                else "EXPIRED"
            )
            db.add(db_prof)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "E-mail, CPF, telefone, Firebase UID ou ID "
                        "já cadastrados no sistema"
                    ),
                )
            return professional_response(db_prof)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="E-mail, CPF, telefone, Firebase UID ou ID já cadastrados no sistema",
        )
    return professional_response(db_prof)


@app.post(
    "/api/v1/auth/login",
    response_model=schemas.ProfessionalResponse,
)
def login(
    firebase_token: dict[str, Any] = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    professional = (
        db.query(models.ProfessionalModel)
        .filter(
            models.ProfessionalModel.firebase_uid == firebase_token["uid"]
        )
        .first()
    )
    if professional is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Não existe um profissional vinculado a este usuário Firebase",
        )
    return professional_response(professional)


@app.post(
    "/api/v1/auth/google",
    response_model=schemas.ProfessionalResponse,
)
def google_login(
    firebase_token: dict[str, Any] = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    professional = (
        db.query(models.ProfessionalModel)
        .filter(
            models.ProfessionalModel.firebase_uid == firebase_token["uid"]
        )
        .first()
    )
    if professional is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Não existe um profissional vinculado a este usuário Firebase",
        )
    return professional_response(professional)


@app.post("/api/v1/auth/password-reset/request")
def request_password_reset(
    reset_request: schemas.PasswordResetRequest,
    db: Session = Depends(get_db),
):
    api_key = os.getenv(FIREBASE_WEB_API_KEY_ENV)
    if not api_key:
        logger.error(
            "%s is not configured; password reset is unavailable",
            FIREBASE_WEB_API_KEY_ENV,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="O serviço de recuperação de senha está indisponível",
        )

    response_message = (
        "Se os dados informados corresponderem a uma conta, "
        "enviaremos instruções para o e-mail cadastrado."
    )
    professional = (
        db.query(models.ProfessionalModel)
        .filter(
            func.lower(models.ProfessionalModel.email) == reset_request.email
        )
        .first()
    )
    if professional is None:
        return {"message": response_message}

    stored_cpf = re.sub(r"\D", "", professional.cpf or "")
    if stored_cpf != reset_request.cpf:
        return {"message": response_message}

    try:
        firebase_response = requests.post(
            "https://identitytoolkit.googleapis.com/v1/"
            f"accounts:sendOobCode?key={quote(api_key, safe='')}",
            json={
                "requestType": "PASSWORD_RESET",
                "email": professional.email,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.error(
            "Firebase password reset request failed (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Não foi possível processar a solicitação. Tente novamente.",
        ) from exc

    if not firebase_response.ok:
        try:
            firebase_error = firebase_response.json().get("error", {}).get("message")
        except (ValueError, AttributeError):
            firebase_error = None

        if firebase_error not in {
            "EMAIL_NOT_FOUND",
            "INVALID_EMAIL",
            "USER_DISABLED",
        }:
            logger.error(
                "Firebase password reset endpoint returned HTTP %s",
                firebase_response.status_code,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "Não foi possível processar a solicitação. "
                    "Tente novamente."
                ),
            )

    return {"message": response_message}


@app.post(
    "/api/v1/subscription/verify",
    response_model=schemas.ProfessionalResponse,
)
def verify_subscription(
    sub_data: schemas.SubscriptionVerifyRequest,
    current_user: models.ProfessionalModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    package_name = os.getenv(GOOGLE_PLAY_PACKAGE_ENV)
    if not package_name:
        logger.error("%s não está configurada", GOOGLE_PLAY_PACKAGE_ENV)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Nome do pacote da Google Play não configurado",
        )

    professional = (
        db.query(models.ProfessionalModel)
        .filter(models.ProfessionalModel.id == current_user.id)
        .with_for_update()
        .first()
    )
    if professional is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profissional não encontrado",
        )
    if not professional.firebase_uid or len(professional.firebase_uid) > 64:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A conta não possui um identificador compatível com o Google Play",
        )

    requested_token = sub_data.purchaseToken
    requested_tokens = {requested_token}
    requested_owners = _subscription_token_owners(db, requested_tokens)
    if requested_owners and requested_owners != {professional.id}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="O token da assinatura já está associado a outra conta",
        )

    token_to_verify = requested_token
    if (
        professional.purchase_token
        and professional.purchase_token != requested_token
        and requested_owners == {professional.id}
    ):
        historical_token_hashes = {
            _purchase_token_hash(token)
            for token in _professional_subscription_tokens(
                db,
                professional,
            )
        }
        has_pending_events = (
            db.query(models.GooglePlayWebhookEventModel)
            .filter(
                models.GooglePlayWebhookEventModel.purchase_token_hash.in_(
                    historical_token_hashes
                ),
                models.GooglePlayWebhookEventModel.status == "PENDING",
            )
            .first()
            is not None
        )
        if not has_pending_events:
            db.refresh(professional)
            return professional_response(professional)
        token_to_verify = professional.purchase_token

    subscription = fetch_google_play_subscription(
        token_to_verify,
        package_name,
    )
    subscription_status, plan_type, expiration = (
        google_play_subscription_snapshot(subscription)
    )
    linked_token = subscription.linkedPurchaseToken
    subscription_tokens = {requested_token, token_to_verify}
    if linked_token:
        subscription_tokens.add(linked_token)

    account_id = _external_account_id(subscription)
    if account_id and account_id != professional.firebase_uid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A compra Google Play não pertence à conta autenticada",
        )

    token_owners = _subscription_token_owners(db, subscription_tokens)
    if token_owners and token_owners != {professional.id}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A assinatura está associada a outra conta",
        )
    if not account_id and not token_owners:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Não foi possível confirmar o vínculo da compra à conta",
        )

    if (
        sub_data.planType is not None
        and sub_data.planType != plan_type
        and requested_owners != {professional.id}
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="O plano informado não corresponde ao produto confirmado pela Google Play",
        )

    if subscription_status == "PENDING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A compra ainda está pendente na Google Play",
        )
    if subscription_status in {"ON_HOLD", "PAUSED", "EXPIRED"} and (
        professional.purchase_token != token_to_verify
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Google Play não confirmou uma assinatura com acesso ativo",
        )
    if (
        subscription_status in {"ACTIVE", "CANCELED"}
        and expiration is not None
        and expiration <= datetime.now(timezone.utc)
        and professional.purchase_token != token_to_verify
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A assinatura confirmada pela Google Play já expirou",
        )
    if (
        subscription_status == "ACTIVE"
        and expiration is not None
        and expiration <= datetime.now(timezone.utc)
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="A Google Play retornou uma assinatura ativa já expirada",
        )

    _record_subscription_tokens(
        db,
        professional,
        subscription_tokens,
    )
    professional.plan_type = plan_type
    professional.subscription_status = subscription_status
    professional.subscription_expires_at = expiration
    professional.purchase_token = token_to_verify
    reconciled_events = _mark_pending_google_play_events_processed(
        db,
        subscription_tokens,
        datetime.now(timezone.utc),
    )
    try:
        db.commit()
        db.refresh(professional)
    except IntegrityError as exc:
        db.rollback()
        logger.warning(
            "Conflito ao associar token de assinatura Google Play"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="O token da assinatura já está associado a outra conta",
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Não foi possível persistir a assinatura confirmada pela "
            "Google Play (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível salvar a assinatura confirmada",
        ) from exc

    reconciled_events += _reconcile_pending_events_after_verification(
        db,
        professional,
        package_name,
    )

    logger.info(
        "Assinatura Google Play sincronizada: status=%s "
        "plano=%s notificacoes_pendentes_reconciliadas=%s",
        professional.subscription_status,
        professional.plan_type,
        reconciled_events,
    )
    return professional_response(professional)


def fetch_google_play_subscription(
    purchase_token: str,
    package_name: str,
) -> schemas.GooglePlaySubscriptionDetails:
    service_account_json = os.getenv(GOOGLE_PLAY_SERVICE_ACCOUNT_ENV)
    if not service_account_json:
        logger.error("%s não está configurada", GOOGLE_PLAY_SERVICE_ACCOUNT_ENV)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Credencial da Google Play não configurada",
        )

    try:
        service_account_info = json.loads(service_account_json)
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        credentials.refresh(GoogleAuthRequest())
    except (ValueError, google.auth.exceptions.GoogleAuthError) as exc:
        logger.error(
            "Não foi possível obter credencial da Google Play (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível autenticar na Google Play Developer API",
        ) from exc

    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/"
        f"applications/{quote(package_name, safe='')}/purchases/subscriptionsv2/"
        f"tokens/{quote(purchase_token, safe='')}"
    )
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {credentials.token}"},
            timeout=(5, 15),
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        upstream_status = (
            exc.response.status_code if exc.response is not None else None
        )
        logger.error(
            "Google Play Developer API rejeitou a consulta (HTTP %s)",
            upstream_status,
        )
        if upstream_status in {400, 404}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A Google Play não reconheceu o token de compra",
            ) from None
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Não foi possível consultar a assinatura na Google Play",
        ) from None
    except requests.RequestException as exc:
        logger.error(
            "Consulta à Google Play Developer API falhou (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Não foi possível consultar a assinatura na Google Play",
        ) from None

    try:
        return schemas.GooglePlaySubscriptionDetails.model_validate(
            response.json()
        )
    except (ValueError, ValidationError) as exc:
        logger.error(
            "Resposta inválida da Google Play Developer API (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="A resposta da Google Play não contém dados válidos",
        ) from exc


@app.post(
    "/api/v1/webhooks/google-play",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_google_play_pubsub_token)],
)
def google_play_webhook(
    payload: schemas.GooglePubSubPushRequest,
    db: Session = Depends(get_db),
):
    try:
        notification_data = base64.b64decode(
            payload.message.data,
            validate=True,
        )
        notification = schemas.GooglePlayDeveloperNotification.model_validate_json(
            notification_data
        )
    except (binascii.Error, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload inválido do Google Pub/Sub",
        ) from exc

    package_name = os.getenv(GOOGLE_PLAY_PACKAGE_ENV)
    if not package_name:
        logger.error("%s não está configurada", GOOGLE_PLAY_PACKAGE_ENV)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Nome do pacote da Google Play não configurado",
        )
    if notification.packageName != package_name:
        logger.warning(
            "Notificação Pub/Sub rejeitada por divergência de pacote"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pacote da notificação não corresponde a esta API",
        )
    if notification.testNotification is not None:
        if notification.subscriptionNotification is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Notificação de teste contém evento de assinatura",
            )
        logger.info(
            "Notificação de teste Google Play reconhecida: event_id=%s",
            payload.message.messageId,
        )
        return {"status": "acknowledged", "result": "test_notification"}

    subscription_notification = notification.subscriptionNotification
    if subscription_notification is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Notificação Google Play sem evento reconhecido",
        )

    event_key = _google_play_event_key(payload.message)
    existing_event = (
        db.query(models.GooglePlayWebhookEventModel)
        .filter_by(event_key=event_key)
        .first()
    )
    if existing_event is not None:
        if existing_event.status in {"PROCESSED", "REJECTED"}:
            return {"status": "acknowledged", "result": "duplicate_event"}
        if not _subscription_token_owners(
            db,
            {subscription_notification.purchaseToken},
        ):
            return {
                "status": "acknowledged",
                "result": "awaiting_account_link",
            }

    try:
        subscription = fetch_google_play_subscription(
            subscription_notification.purchaseToken,
            package_name,
        )
    except HTTPException as exc:
        if exc.status_code != status.HTTP_422_UNPROCESSABLE_ENTITY:
            raise
        return _record_rejected_google_play_event(
            db,
            event_key,
            subscription_notification,
            notification.eventTimeMillis,
            "purchase_token_not_recognized",
        )
    subscription_status, plan_type, expiration = (
        google_play_subscription_snapshot(subscription)
    )
    notification_token = subscription_notification.purchaseToken
    linked_token = subscription.linkedPurchaseToken
    subscription_tokens = {notification_token}
    if linked_token:
        subscription_tokens.add(linked_token)

    try:
        professional = _resolve_subscription_professional(
            db,
            subscription,
            subscription_tokens,
        )
    except HTTPException as exc:
        if exc.status_code in {
            status.HTTP_403_FORBIDDEN,
            status.HTTP_409_CONFLICT,
        }:
            return _record_rejected_google_play_event(
                db,
                event_key,
                subscription_notification,
                notification.eventTimeMillis,
                "account_association_conflict",
            )
        if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
            raise
        try:
            _persist_pending_google_play_event(
                db,
                event_key,
                notification_token,
                subscription_notification.notificationType,
                notification.eventTimeMillis,
            )
        except SQLAlchemyError as db_exc:
            logger.error(
                "Não foi possível persistir evento Google Play pendente: "
                "event_id=%s erro=%s",
                event_key,
                type(db_exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Não foi possível registrar a notificação Google Play",
            ) from db_exc
        try:
            professional = _resolve_subscription_professional(
                db,
                subscription,
                subscription_tokens,
            )
        except HTTPException as retry_exc:
            if retry_exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
                raise
            logger.info(
                "Notificação Google Play aguardando associação segura: "
                "event_id=%s",
                event_key,
            )
            return {
                "status": "acknowledged",
                "result": "awaiting_account_link",
            }

    current_token = professional.purchase_token
    incoming_owners = _subscription_token_owners(
        db,
        {notification_token},
    )
    if (
        current_token
        and current_token != notification_token
        and incoming_owners == {professional.id}
        and linked_token != current_token
    ):
        try:
            subscription = fetch_google_play_subscription(
                current_token,
                package_name,
            )
        except HTTPException as exc:
            if exc.status_code != status.HTTP_422_UNPROCESSABLE_ENTITY:
                raise
            return _record_rejected_google_play_event(
                db,
                event_key,
                subscription_notification,
                notification.eventTimeMillis,
                "current_purchase_token_not_recognized",
            )
        subscription_status, plan_type, expiration = (
            google_play_subscription_snapshot(subscription)
        )
        notification_token = current_token
        linked_token = subscription.linkedPurchaseToken
        subscription_tokens = {notification_token}
        if linked_token:
            subscription_tokens.add(linked_token)

        current_account_id = _external_account_id(subscription)
        if (
            current_account_id
            and current_account_id != professional.firebase_uid
        ):
            return _record_rejected_google_play_event(
                db,
                event_key,
                subscription_notification,
                notification.eventTimeMillis,
                "account_association_conflict",
            )

    if subscription_status == "PENDING":
        _mark_google_play_event_processed(
            db,
            event_key,
            subscription_notification,
            notification.eventTimeMillis,
        )
        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error(
                "Não foi possível registrar evento de compra pendente: "
                "event_id=%s erro=%s",
                event_key,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Não foi possível registrar a notificação Google Play",
            ) from exc
        logger.info(
            "Notificação de compra pendente não alterou o acesso: "
            "event_id=%s event_time_ms=%s event_type=%s",
            event_key,
            notification.eventTimeMillis,
            subscription_notification.notificationType,
        )
        return {"status": "acknowledged", "result": "purchase_pending"}

    token_owners = _subscription_token_owners(db, subscription_tokens)
    if token_owners and token_owners != {professional.id}:
        return _record_rejected_google_play_event(
            db,
            event_key,
            subscription_notification,
            notification.eventTimeMillis,
            "account_association_conflict",
        )

    try:
        _record_subscription_tokens(db, professional, subscription_tokens)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_409_CONFLICT:
            raise
        return _record_rejected_google_play_event(
            db,
            event_key,
            subscription_notification,
            notification.eventTimeMillis,
            "account_association_conflict",
        )
    professional.purchase_token = notification_token
    professional.subscription_status = subscription_status
    professional.plan_type = plan_type
    professional.subscription_expires_at = expiration
    _mark_google_play_event_processed(
        db,
        event_key,
        subscription_notification,
        notification.eventTimeMillis,
    )
    try:
        db.commit()
        db.refresh(professional)
    except IntegrityError:
        db.rollback()
        committed_event = (
            db.query(models.GooglePlayWebhookEventModel)
            .filter_by(event_key=event_key)
            .first()
        )
        if (
            committed_event is not None
            and committed_event.status == "PROCESSED"
        ):
            return {"status": "acknowledged", "result": "duplicate_event"}
        return _record_rejected_google_play_event(
            db,
            event_key,
            subscription_notification,
            notification.eventTimeMillis,
            "account_association_conflict",
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Não foi possível persistir notificação Google Play: "
            "event_id=%s event_type=%s erro=%s",
            event_key,
            subscription_notification.notificationType,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível atualizar a assinatura",
        ) from exc

    logger.info(
        "Notificação Google Play processada: event_id=%s event_type=%s "
        "event_time_ms=%s status=%s plano=%s",
        event_key,
        subscription_notification.notificationType,
        notification.eventTimeMillis,
        professional.subscription_status,
        professional.plan_type,
    )
    return {"status": "acknowledged", "result": "subscription_synchronized"}


def restore_firebase_email(firebase_uid: str, email: str, firebase_app) -> None:
    try:
        auth.update_user(firebase_uid, email=email, app=firebase_app)
    except exceptions.FirebaseError as exc:
        logger.exception(
            "Não foi possível restaurar o e-mail do usuário Firebase %s",
            firebase_uid,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível sincronizar o e-mail do perfil",
        ) from exc

@app.get(
    "/api/v1/auth/profile",
    response_model=schemas.ProfessionalResponse,
)
def get_profile(
    current_user: models.ProfessionalModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        db.refresh(current_user)
    except SQLAlchemyError as exc:
        logger.exception(
            "Não foi possível recarregar o perfil do profissional %s",
            current_user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível carregar o perfil atualizado",
        ) from exc

    return professional_response(current_user)

@app.patch("/api/v1/auth/profile")
def update_profile(
    profile: schemas.UserProfileUpdate,
    current_user: models.ProfessionalModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    updates = profile.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Informe ao menos um campo para atualização",
        )

    if "email" in updates and updates["email"] is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O e-mail não pode ser nulo",
        )
    if "email" in updates and not updates["email"].strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O e-mail não pode estar vazio",
        )
    if "specialty" in updates and updates["specialty"] is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A especialidade não pode ser nula",
        )
    if "specialty" in updates and not updates["specialty"].strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A especialidade não pode estar vazia",
        )
    if "name" in updates and updates["name"] is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O nome não pode ser nulo",
        )
    if "name" in updates and not updates["name"].strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O nome não pode estar vazio",
        )

    duplicate_conditions = []
    if updates.get("email") and updates["email"] != current_user.email:
        duplicate_conditions.append(
            models.ProfessionalModel.email == updates["email"]
        )
    phone_number = updates.get("phone_number")
    if phone_number and phone_number != current_user.phone:
        duplicate_conditions.append(
            models.ProfessionalModel.phone == phone_number
        )
    if duplicate_conditions and db.query(models.ProfessionalModel.id).filter(
        models.ProfessionalModel.id != current_user.id,
        or_(*duplicate_conditions),
    ).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="E-mail ou telefone já cadastrado no sistema",
        )

    firebase_email_updated = False
    firebase_app = None
    firebase_uid = current_user.firebase_uid
    previous_email = current_user.email
    requested_email = updates.get("email")
    if requested_email is not None and requested_email != previous_email:
        if not firebase_uid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="O usuário não possui um UID Firebase vinculado",
            )
        firebase_app = firebase_admin.get_app()
        try:
            firebase_user = auth.get_user(firebase_uid, app=firebase_app)
        except auth.UserNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Usuário não encontrado no Firebase",
            ) from exc
        except exceptions.FirebaseError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Não foi possível consultar o usuário no Firebase",
            ) from exc

        if any(
            provider.provider_id == "google.com"
            for provider in firebase_user.provider_data
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Contas do Google não podem alterar o e-mail por esta via",
            )
        try:
            auth.update_user(firebase_uid, email=requested_email, app=firebase_app)
            firebase_email_updated = True
        except auth.EmailAlreadyExistsError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="E-mail já cadastrado no Firebase",
            ) from exc
        except exceptions.InvalidArgumentError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="E-mail inválido",
            ) from exc
        except auth.UserNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Usuário não encontrado no Firebase",
            ) from exc
        except exceptions.FirebaseError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Não foi possível atualizar o e-mail no Firebase",
            ) from exc

    profile_field_mapping = {
        "name": "name",
        "phone_number": "phone",
        "specialty": "specialty",
        "specialty_id": "specialty_id",
        "professional_register": "professional_register",
        "signature_url": "signature_url",
    }
    for request_field, model_field in profile_field_mapping.items():
        if request_field in updates:
            setattr(current_user, model_field, updates[request_field])
    if "email" in updates:
        current_user.email = requested_email

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if firebase_email_updated and firebase_uid and firebase_app:
            restore_firebase_email(firebase_uid, previous_email, firebase_app)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="E-mail ou telefone já cadastrado no sistema",
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        if firebase_email_updated and firebase_uid and firebase_app:
            restore_firebase_email(firebase_uid, previous_email, firebase_app)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível salvar as alterações do perfil",
        ) from exc

    try:
        db.refresh(current_user)
    except SQLAlchemyError as exc:
        logger.exception(
            "Os dados do perfil foram salvos, mas não puderam ser recarregados "
            "para o profissional %s",
            current_user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Os dados foram salvos, mas não foi possível recarregar o perfil",
        ) from exc

    return professional_response(current_user)

@app.delete("/api/v1/auth/delete-account")
def delete_account(
    current_user: models.ProfessionalModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    firebase_uid = current_user.firebase_uid
    if not firebase_uid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="O usuário não possui um UID Firebase vinculado",
        )

    db.query(models.EvolutionRecordModel).filter(
        models.EvolutionRecordModel.professional_id == current_user.id,
    ).delete(synchronize_session=False)
    db.query(models.AppointmentModel).filter(
        models.AppointmentModel.professional_id == current_user.id,
    ).delete(synchronize_session=False)
    db.query(models.PatientModel).filter(
        models.PatientModel.professional_id == current_user.id,
    ).delete(synchronize_session=False)
    db.query(models.SubscriptionPurchaseTokenModel).filter(
        models.SubscriptionPurchaseTokenModel.professional_id
        == current_user.id,
    ).delete(synchronize_session=False)
    db.query(models.ProfessionalModel).filter(
        models.ProfessionalModel.id == current_user.id,
    ).delete(synchronize_session=False)

    try:
        firebase_app = firebase_admin.get_app()
        auth.delete_user(firebase_uid, app=firebase_app)
    except auth.UserNotFoundError:
        logger.info(
            "Usuário Firebase %s já foi removido; prosseguindo com exclusão local",
            firebase_uid,
        )
    except exceptions.FirebaseError as exc:
        db.rollback()
        logger.exception(
            "Não foi possível remover o usuário Firebase %s",
            firebase_uid,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Não foi possível excluir a conta no Firebase",
        ) from exc

    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            "Não foi possível excluir os dados locais do profissional %s",
            current_user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível excluir os dados da conta",
        ) from exc
    return {"status": "success", "message": "Conta e dados excluídos com sucesso"}

@app.get(
    "/api/v1/patients/{professional_id}",
    dependencies=[Depends(require_active_professional)],
)
def get_patients(
    professional_id: str,
    updated_since: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
):
    _require_timezone_aware_updated_since(updated_since)
    query = db.query(models.PatientModel).filter(
        models.PatientModel.professional_id == professional_id
    )
    if updated_since is not None:
        query = query.filter(models.PatientModel.updated_at >= updated_since)
    patients = query.all()
    result = []
    for p in patients:
        result.append({
            "id": p.id,
            "professionalId": p.professional_id,
            "fullName": p.full_name,
            "motherName": p.mother_name,
            "birthDateEpochMillis": p.birth_date_epoch,
            "phone": p.phone,
            "email": p.email,
            "guardianName": p.guardian_name,
            "relationship": p.relationship,
            "notes": p.notes,
            "createdAtEpochMillis": p.created_at_epoch,
            "updatedAt": p.updated_at,
        })
    return result

@app.post(
    "/api/v1/patients/{professional_id}",
    dependencies=[Depends(require_active_professional)],
)
def sync_patient(
    professional_id: str,
    patient: schemas.PatientSchema,
    db: Session = Depends(get_db),
):
    if patient.professionalId != professional_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="O paciente deve pertencer ao profissional informado",
        )

    p = (
        db.query(models.PatientModel)
        .filter(
            models.PatientModel.id == patient.id,
            models.PatientModel.professional_id == professional_id,
        )
        .first()
    )
    if p:
        p.full_name = patient.fullName
        p.mother_name = patient.motherName
        p.birth_date_epoch = patient.birthDateEpochMillis
        p.phone = patient.phone
        p.email = patient.email
        p.guardian_name = patient.guardianName
        p.relationship = patient.relationship
        p.notes = patient.notes
    else:
        owned_by_another_professional = (
            db.query(models.PatientModel.id)
            .filter(
                models.PatientModel.id == patient.id,
                models.PatientModel.professional_id != professional_id,
            )
            .first()
        )
        if owned_by_another_professional:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="O ID do paciente pertence a outro profissional",
            )

        p = models.PatientModel(
            id=patient.id,
            professional_id=professional_id,
            full_name=patient.fullName,
            mother_name=patient.motherName,
            birth_date_epoch=patient.birthDateEpochMillis,
            phone=patient.phone,
            email=patient.email,
            guardian_name=patient.guardianName,
            relationship=patient.relationship,
            notes=patient.notes,
            created_at_epoch=patient.createdAtEpochMillis
        )
        db.add(p)
    db.commit()
    db.refresh(p)
    patient.updatedAt = p.updated_at
    return patient

@app.delete(
    "/api/v1/patients/{professional_id}/{patientId}",
    dependencies=[Depends(require_active_professional)],
)
def delete_patient(
    professional_id: str = Path(...),
    patient_id: str = Path(..., alias="patientId"),
    db: Session = Depends(get_db),
):
    patient = (
        db.query(models.PatientModel)
        .filter(
            models.PatientModel.id == patient_id,
            models.PatientModel.professional_id == professional_id,
        )
        .first()
    )
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Paciente não encontrado",
        )

    db.query(models.EvolutionRecordModel).filter(
        models.EvolutionRecordModel.patient_id == patient_id,
    ).delete(synchronize_session=False)
    db.query(models.AppointmentModel).filter(
        models.AppointmentModel.patient_id == patient_id,
    ).delete(synchronize_session=False)
    db.delete(patient)
    db.commit()
    return {"status": "success"}

@app.get(
    "/api/v1/appointments/{professional_id}",
    response_model=list[AppointmentSchema],
    dependencies=[Depends(require_active_professional)],
)
def get_appointments(
    professional_id: str = Path(...),
    updated_since: Optional[datetime] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    month: Optional[int] = Query(None, ge=1, le=12),
    year: Optional[int] = Query(None, ge=1, le=9998),
    db: Session = Depends(get_db),
):
    _require_timezone_aware_updated_since(updated_since)
    has_date_range = start_date is not None or end_date is not None
    has_month_filter = month is not None or year is not None
    if has_date_range and has_month_filter:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Use start_date/end_date ou month/year, não ambos",
        )
    if has_date_range and (start_date is None or end_date is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Informe start_date e end_date juntos",
        )
    if has_month_filter and (month is None or year is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Informe month e year juntos",
        )

    query = db.query(models.AppointmentModel).filter(
        models.AppointmentModel.professional_id == professional_id
    )
    if updated_since is not None:
        query = query.filter(
            models.AppointmentModel.updated_at >= updated_since
        )
    if start_date is not None and end_date is not None:
        if end_date < start_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="end_date deve ser igual ou posterior a start_date",
            )
        if end_date == date.max:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="end_date está fora do intervalo suportado",
            )
        range_start = datetime.combine(
            start_date,
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        range_end = datetime.combine(
            end_date + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
    elif month is not None and year is not None:
        range_start = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            range_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            range_end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    else:
        range_start = None
        range_end = None

    if range_start is not None and range_end is not None:
        query = query.filter(
            models.AppointmentModel.date_time_epoch
            >= int(range_start.timestamp()) * 1000,
            models.AppointmentModel.date_time_epoch
            < int(range_end.timestamp()) * 1000,
        )

    appointments = query.order_by(
        models.AppointmentModel.date_time_epoch
    ).all()
    return [
        {
            "id": appointment.id,
            "professionalId": appointment.professional_id,
            "patientId": appointment.patient_id,
            "patientName": appointment.patient_name,
            "recurrenceId": appointment.recurrence_id,
            "recurrenceRuleId": appointment.recurrence_rule_id,
            "dateTimeEpoch": appointment.date_time_epoch,
            "durationMinutes": appointment.duration_minutes,
            "status": appointment.status,
            "notes": appointment.notes,
            "updatedAt": appointment.updated_at,
        }
        for appointment in appointments
    ]

@app.post(
    "/api/v1/appointments/{professional_id}",
    response_model=list[AppointmentSchema],
    dependencies=[Depends(require_active_professional)],
)
def sync_appointments(
    professional_id: str = Path(...),
    appointments: list[AppointmentSchema] = Body(...),
    db: Session = Depends(get_db),
):
    appointment_ids = [appointment.id for appointment in appointments]
    if len(appointment_ids) != len(set(appointment_ids)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A lista contém IDs de agendamento duplicados",
        )
    if any(appointment.professionalId != professional_id for appointment in appointments):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Todos os agendamentos devem pertencer ao profissional informado",
        )

    existing_appointments = (
        db.query(models.AppointmentModel)
        .filter(
            models.AppointmentModel.id.in_(appointment_ids),
            models.AppointmentModel.professional_id == professional_id,
        )
        .all()
    )
    appointments_by_id = {appointment.id: appointment for appointment in existing_appointments}
    foreign_appointments = (
        db.query(models.AppointmentModel.id)
        .filter(
            models.AppointmentModel.id.in_(appointment_ids),
            models.AppointmentModel.professional_id != professional_id,
        )
        .first()
    )
    if foreign_appointments:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Um ou mais IDs de agendamento pertencem a outro profissional",
        )

    patient_ids = {appointment.patientId for appointment in appointments}
    foreign_patients = (
        db.query(models.PatientModel.id)
        .filter(
            models.PatientModel.id.in_(patient_ids),
            models.PatientModel.professional_id != professional_id,
        )
        .first()
    )
    if foreign_patients:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Um ou mais pacientes pertencem a outro profissional",
        )

    synced_appointments = []
    for appointment in appointments:
        db_appointment = appointments_by_id.get(appointment.id)
        if db_appointment is None:
            db_appointment = models.AppointmentModel(id=appointment.id)
            db.add(db_appointment)
            appointments_by_id[appointment.id] = db_appointment

        db_appointment.professional_id = professional_id
        db_appointment.patient_id = appointment.patientId
        db_appointment.patient_name = appointment.patientName
        db_appointment.recurrence_id = appointment.recurrenceId
        db_appointment.recurrence_rule_id = appointment.recurrenceRuleId
        db_appointment.date_time_epoch = appointment.dateTimeEpoch
        db_appointment.duration_minutes = appointment.durationMinutes
        db_appointment.status = appointment.status
        db_appointment.notes = appointment.notes
        synced_appointments.append((appointment, db_appointment))

    db.commit()
    for appointment, db_appointment in synced_appointments:
        db.refresh(db_appointment)
        appointment.updatedAt = db_appointment.updated_at
    return appointments

@app.delete(
    "/api/v1/appointments/{professional_id}/{appointmentId}",
    dependencies=[Depends(require_active_professional)],
)
def delete_appointment(
    professional_id: str = Path(...),
    appointment_id: str = Path(..., alias="appointmentId"),
    delete_all: bool = Query(False, alias="deleteAll"),
    db: Session = Depends(get_db),
):
    appointment = (
        db.query(models.AppointmentModel)
        .filter(
            models.AppointmentModel.id == appointment_id,
            models.AppointmentModel.professional_id == professional_id,
        )
        .first()
    )
    if appointment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agendamento não encontrado",
        )

    appointments_query = db.query(models.AppointmentModel).filter(
        models.AppointmentModel.professional_id == professional_id,
    )
    if delete_all and appointment.recurrence_id is not None:
        appointments_query = appointments_query.filter(
            models.AppointmentModel.recurrence_id == appointment.recurrence_id,
        )
    else:
        appointments_query = appointments_query.filter(
            models.AppointmentModel.id == appointment_id,
        )

    appointments_to_delete = appointments_query.with_for_update().all()
    deletable_statuses = {"PENDING", "SCHEDULED", "CONFIRMED"}
    deletable_appointments = [
        item
        for item in appointments_to_delete
        if item.status in deletable_statuses
    ]
    appointment_ids = [item.id for item in deletable_appointments]

    if appointment_ids:
        db.query(models.EvolutionRecordModel).filter(
            models.EvolutionRecordModel.appointment_id.in_(appointment_ids),
        ).delete(synchronize_session=False)
        appointments_query.filter(
            models.AppointmentModel.id.in_(appointment_ids),
            models.AppointmentModel.status.in_(deletable_statuses),
        ).delete(synchronize_session=False)

    db.commit()
    return {
        "status": "success",
        "deletedCount": len(appointment_ids),
        "preservedCount": len(appointments_to_delete) - len(appointment_ids),
    }

@app.get(
    "/api/v1/evolutions/{professional_id}",
    response_model=list[EvolutionRecordSchema],
    dependencies=[Depends(require_active_professional)],
)
def get_evolutions(
    professional_id: str = Path(...),
    updated_since: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
):
    _require_timezone_aware_updated_since(updated_since)
    query = db.query(models.EvolutionRecordModel).filter(
        models.EvolutionRecordModel.professional_id == professional_id
    )
    if updated_since is not None:
        query = query.filter(
            models.EvolutionRecordModel.updated_at >= updated_since
        )
    evolutions = query.all()
    return [
        {
            "id": evolution.id,
            "appointmentId": evolution.appointment_id,
            "patientId": evolution.patient_id,
            "professionalId": evolution.professional_id,
            "dateEpoch": evolution.date_epoch,
            "activityPerformed": evolution.activity_performed,
            "performanceMetrics": evolution.performance_metrics,
            "textualEvolution": evolution.textual_evolution,
            "observations": evolution.observations,
            "customFields": evolution.custom_fields,
            "updatedAt": evolution.updated_at,
        }
        for evolution in evolutions
    ]

@app.delete(
    "/api/v1/evolutions/{professional_id}/{evolutionId}",
    dependencies=[Depends(require_active_professional)],
)
def delete_evolution(
    professional_id: str = Path(...),
    evolution_id: str = Path(..., alias="evolutionId"),
    db: Session = Depends(get_db),
):
    evolution = (
        db.query(models.EvolutionRecordModel)
        .filter(
            models.EvolutionRecordModel.id == evolution_id,
            models.EvolutionRecordModel.professional_id == professional_id,
        )
        .first()
    )
    if evolution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Evolução não encontrada",
        )

    db.delete(evolution)
    db.commit()
    return {"status": "success"}

@app.post(
    "/api/v1/evolutions/{professional_id}",
    response_model=list[EvolutionRecordSchema],
    dependencies=[Depends(require_active_professional)],
)
def sync_evolutions(
    professional_id: str = Path(...),
    evolutions: list[EvolutionRecordSchema] = Body(...),
    db: Session = Depends(get_db),
):
    evolution_ids = [evolution.id for evolution in evolutions]
    if len(evolution_ids) != len(set(evolution_ids)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A lista contém IDs de evolução duplicados",
        )
    if any(evolution.professionalId != professional_id for evolution in evolutions):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Todas as evoluções devem pertencer ao profissional informado",
        )

    existing_evolutions = (
        db.query(models.EvolutionRecordModel)
        .filter(
            models.EvolutionRecordModel.id.in_(evolution_ids),
            models.EvolutionRecordModel.professional_id == professional_id,
        )
        .all()
    )
    evolutions_by_id = {evolution.id: evolution for evolution in existing_evolutions}
    foreign_evolutions = (
        db.query(models.EvolutionRecordModel.id)
        .filter(
            models.EvolutionRecordModel.id.in_(evolution_ids),
            models.EvolutionRecordModel.professional_id != professional_id,
        )
        .first()
    )
    if foreign_evolutions:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Um ou mais IDs de evolução pertencem a outro profissional",
        )

    patient_ids = {evolution.patientId for evolution in evolutions}
    foreign_patients = (
        db.query(models.PatientModel.id)
        .filter(
            models.PatientModel.id.in_(patient_ids),
            models.PatientModel.professional_id != professional_id,
        )
        .first()
    )
    if foreign_patients:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Um ou mais pacientes pertencem a outro profissional",
        )

    appointment_ids = {
        evolution.appointmentId
        for evolution in evolutions
        if evolution.appointmentId is not None
    }
    foreign_linked_appointments = (
        db.query(models.AppointmentModel.id)
        .filter(
            models.AppointmentModel.id.in_(appointment_ids),
            models.AppointmentModel.professional_id != professional_id,
        )
        .first()
    )
    if foreign_linked_appointments:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Um ou mais agendamentos pertencem a outro profissional",
        )

    synced_evolutions = []
    for evolution in evolutions:
        db_evolution = evolutions_by_id.get(evolution.id)
        if db_evolution is None:
            db_evolution = models.EvolutionRecordModel(id=evolution.id)
            db.add(db_evolution)
            evolutions_by_id[evolution.id] = db_evolution

        db_evolution.appointment_id = evolution.appointmentId
        db_evolution.patient_id = evolution.patientId
        db_evolution.professional_id = professional_id
        db_evolution.date_epoch = evolution.dateEpoch
        db_evolution.activity_performed = evolution.activityPerformed
        db_evolution.performance_metrics = evolution.performanceMetrics
        db_evolution.textual_evolution = evolution.textualEvolution
        db_evolution.observations = evolution.observations
        db_evolution.custom_fields = evolution.customFields
        synced_evolutions.append((evolution, db_evolution))

    db.commit()
    for evolution, db_evolution in synced_evolutions:
        db.refresh(db_evolution)
        evolution.updatedAt = db_evolution.updated_at
    return evolutions