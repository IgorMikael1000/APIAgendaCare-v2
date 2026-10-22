import asyncio
import base64
import binascii
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
FIREBASE_WEB_API_KEY_ENV = "FIREBASE_WEB_API_KEY"


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
        "trialEndsAt": format_expiration_date(prof.trial_ends_at),
        "subscriptionExpiresAt": format_expiration_date(
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
    if subscription_status == "ACTIVE":
        return prof.subscription_expires_at
    return prof.subscription_expires_at or prof.trial_ends_at


def is_subscription_expired(
    prof: models.ProfessionalModel,
    now: Optional[datetime] = None,
) -> bool:
    subscription_status = (prof.subscription_status or "").upper()
    if subscription_status not in {"TRIAL", "ACTIVE", "CANCELED"}:
        return True

    expiration = subscription_expiration(prof)
    if expiration is None:
        return True

    current_time = _as_utc(now or datetime.now(timezone.utc))
    return _as_utc(expiration) <= current_time


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
        activation_date = _as_utc(existing_device.data_ativacao)
    else:
        activation_date = current_date
        db.add(
            models.TrialDeviceModel(
                android_id=prof.android_id,
                data_ativacao=activation_date,
            )
        )

    trial_ends_at = activation_date + timedelta(days=7)
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


@app.post("/api/v1/subscription/verify")
def verify_subscription(
    sub_data: schemas.SubscriptionVerifyRequest,
    current_user: models.ProfessionalModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    plan_durations = {
        "MONTHLY": 30,
        "QUARTERLY": 90,
        "SEMIANNUAL": 180,
        "ANNUAL": 365,
    }
    expiration = datetime.now(timezone.utc) + timedelta(
        days=plan_durations[sub_data.planType]
    )
    current_user.subscription_status = "ACTIVE"
    current_user.plan_type = sub_data.planType
    current_user.subscription_expires_at = expiration
    current_user.purchase_token = sub_data.purchaseToken
    db.commit()

    return {
        "status": "success",
        "message": "Assinatura ativada com sucesso",
        "subscriptionExpiresAt": expiration,
        "bloquear_app": is_subscription_expired(current_user),
    }


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
        logger.exception("Não foi possível obter credencial da Google Play")
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
    except requests.RequestException as exc:
        logger.error(
            "Consulta à Google Play Developer API falhou (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Não foi possível consultar a assinatura na Google Play",
        ) from exc

    try:
        return schemas.GooglePlaySubscriptionDetails.model_validate(
            response.json()
        )
    except (ValueError, ValidationError) as exc:
        logger.exception("Resposta inválida da Google Play Developer API")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="A resposta da Google Play não contém dados válidos",
        ) from exc


@app.post("/api/v1/webhooks/google-play", status_code=status.HTTP_200_OK)
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

    subscription_notification = notification.subscriptionNotification
    if subscription_notification is None:
        return {"status": "acknowledged"}

    package_name = (
        notification.packageName or os.getenv(GOOGLE_PLAY_PACKAGE_ENV)
    )
    if not package_name:
        logger.error("%s não está configurada", GOOGLE_PLAY_PACKAGE_ENV)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Nome do pacote da Google Play não configurado",
        )

    subscription = fetch_google_play_subscription(
        subscription_notification.purchaseToken,
        package_name,
    )
    plan_types = {
        "plan_monthly": "MONTHLY",
        "plan_quarterly": "QUARTERLY",
        "plan_semiannual": "SEMIANNUAL",
        "plan_annual": "ANNUAL",
    }
    subscription_statuses = {
        "SUBSCRIPTION_STATE_ACTIVE": "ACTIVE",
        "SUBSCRIPTION_STATE_IN_GRACE_PERIOD": "ACTIVE",
        "SUBSCRIPTION_STATE_CANCELED": "CANCELED",
        "SUBSCRIPTION_STATE_ON_HOLD": "ON_HOLD",
        "SUBSCRIPTION_STATE_PAUSED": "PAUSED",
        "SUBSCRIPTION_STATE_EXPIRED": "EXPIRED",
        "SUBSCRIPTION_STATE_PENDING": "PENDING",
        "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED": "EXPIRED",
    }

    if subscription.subscriptionState not in subscription_statuses:
        logger.error(
            "Estado de assinatura Google Play não suportado: %s",
            subscription.subscriptionState,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Estado de assinatura da Google Play não reconhecido",
        )

    valid_line_items = [
        item
        for item in subscription.lineItems
        if item.productId in plan_types and item.expiryTime is not None
    ]
    if not valid_line_items:
        logger.error(
            "Google Play retornou assinatura sem SKU conhecido ou expiração"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="A assinatura da Google Play não contém plano ou expiração válidos",
        )
    current_line_item = max(
        valid_line_items,
        key=lambda item: item.expiryTime or datetime.min.replace(
            tzinfo=timezone.utc
        ),
    )

    linked_tokens = {
        subscription_notification.purchaseToken,
        subscription.linkedPurchaseToken,
    }
    linked_tokens.discard(None)
    account_id = (
        subscription.externalAccountIdentifiers.obfuscatedExternalAccountId
        if subscription.externalAccountIdentifiers is not None
        else None
    )
    professional = None
    if account_id:
        professional = (
            db.query(models.ProfessionalModel)
            .filter(
                models.ProfessionalModel.firebase_uid == account_id
            )
            .first()
        )
        if professional is None:
            professional = (
                db.query(models.ProfessionalModel)
                .filter(models.ProfessionalModel.id == account_id)
                .first()
            )
    if professional is None:
        professional = (
            db.query(models.ProfessionalModel)
            .filter(
                models.ProfessionalModel.purchase_token.in_(linked_tokens)
            )
            .first()
        )
    if professional is None:
        logger.warning(
            "Nenhum profissional corresponde à notificação Google Play "
            "(subscriptionId=%s)",
            subscription_notification.subscriptionId,
        )
        return {"status": "acknowledged", "result": "professional_not_found"}

    professional.purchase_token = subscription_notification.purchaseToken
    professional.subscription_status = subscription_statuses[
        subscription.subscriptionState
    ]
    professional.plan_type = plan_types[current_line_item.productId]
    professional.subscription_expires_at = current_line_item.expiryTime
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            "Não foi possível atualizar a assinatura do profissional %s",
            professional.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível atualizar a assinatura",
        ) from exc

    return {"status": "acknowledged"}


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