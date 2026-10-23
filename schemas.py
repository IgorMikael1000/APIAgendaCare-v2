import re
from datetime import datetime
from pydantic import AliasChoices, BaseModel, Field, field_validator
from typing import Optional, Dict, Any
from typing import Literal


class AppSettingsResponse(BaseModel):
    latest_version_code: int
    force_update: bool


class ProfessionalCreate(BaseModel):
    id: str = Field(min_length=1)
    android_id: str = Field(
        min_length=6,
        max_length=255,
        validation_alias=AliasChoices("android_id", "androidId", "deviceId"),
    )
    name: str
    email: str
    specialty: str
    specialtyId: Optional[str] = None
    professionalRegister: Optional[str] = None
    cpf: str = Field(min_length=1)
    phone: Optional[str] = None
    signatureUrl: Optional[str] = None

    @field_validator("android_id", mode="before")
    @classmethod
    def normalize_android_id(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    cpf: str = Field(min_length=1, max_length=32)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if (
            normalized.count("@") != 1
            or any(character.isspace() for character in normalized)
            or not all(normalized.split("@"))
        ):
            raise ValueError("Informe um e-mail válido")
        return normalized

    @field_validator("cpf")
    @classmethod
    def normalize_cpf(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9.\-/\s]+", value):
            raise ValueError("Informe um CPF válido")
        digits = re.sub(r"\D", "", value)
        if len(digits) != 11:
            raise ValueError("Informe um CPF válido")
        return digits


class ProfessionalResponse(BaseModel):
    id: str
    name: str
    email: str
    specialty: str
    cpf: str
    planType: Optional[str] = None
    subscriptionStatus: Optional[str] = None
    trialEndsAt: Optional[str] = Field(
        default=None,
        description="UTC ISO 8601 timestamp with timezone offset",
    )
    subscriptionExpiresAt: Optional[str] = Field(
        default=None,
        description="UTC ISO 8601 timestamp with timezone offset",
    )
    expirationDate: Optional[str] = Field(
        default=None,
        description="Legacy UTC date-only value; use timestamp fields instead",
    )
    isSubscriptionExpired: bool
    bloquear_app: bool
    specialtyId: Optional[str] = None
    professionalRegister: Optional[str] = None
    phone: Optional[str] = None

class UserProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone_number: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("phone", "phone_number"),
    )
    specialty: Optional[str] = None
    email: Optional[str] = None
    specialty_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("specialtyId", "specialty_id"),
    )
    professional_register: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "professionalRegister",
            "professional_register",
        ),
    )
    signature_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("signatureUrl", "signature_url"),
    )

class SubscriptionVerifyRequest(BaseModel):
    purchaseToken: str = Field(
        min_length=1,
        validation_alias=AliasChoices("purchaseToken", "purchase_token"),
    )
    planType: Optional[
        Literal["MONTHLY", "QUARTERLY", "SEMIANNUAL", "ANNUAL"]
    ] = None


class GooglePubSubMessage(BaseModel):
    data: str = Field(min_length=1)
    messageId: Optional[str] = None


class GooglePubSubPushRequest(BaseModel):
    message: GooglePubSubMessage


class GooglePlaySubscriptionNotification(BaseModel):
    version: str
    notificationType: int
    purchaseToken: str = Field(min_length=1)
    subscriptionId: str = Field(min_length=1)


class GooglePlayTestNotification(BaseModel):
    version: str


class GooglePlayDeveloperNotification(BaseModel):
    version: str
    packageName: Optional[str] = None
    eventTimeMillis: Optional[str] = None
    subscriptionNotification: Optional[GooglePlaySubscriptionNotification] = None
    testNotification: Optional[GooglePlayTestNotification] = None


class GooglePlayLineItem(BaseModel):
    productId: str
    expiryTime: Optional[datetime] = None


class GooglePlayExternalAccountIdentifiers(BaseModel):
    obfuscatedExternalAccountId: Optional[str] = Field(
        default=None,
        max_length=64,
    )


class GooglePlaySubscriptionDetails(BaseModel):
    subscriptionState: str
    linkedPurchaseToken: Optional[str] = None
    externalAccountIdentifiers: Optional[GooglePlayExternalAccountIdentifiers] = None
    lineItems: list[GooglePlayLineItem]


class AppointmentSchema(BaseModel):
    id: str = Field(min_length=1)
    professionalId: str = Field(min_length=1)
    patientId: str = Field(min_length=1)
    patientName: str
    recurrenceId: Optional[str] = None
    recurrenceRuleId: Optional[str] = None
    dateTimeEpoch: int
    durationMinutes: int
    status: Literal[
        "SCHEDULED",
        "CONFIRMED",
        "COMPLETED",
        "PENDING",
        "ABSENT",
        "CANCELLED",
    ]
    notes: Optional[str] = None
    updatedAt: Optional[datetime] = None

class EvolutionRecordSchema(BaseModel):
    id: str = Field(min_length=1)
    appointmentId: Optional[str] = Field(default=None, min_length=1)
    patientId: str = Field(min_length=1)
    professionalId: str = Field(min_length=1)
    dateEpoch: int
    activityPerformed: str
    performanceMetrics: Optional[str] = None
    textualEvolution: str
    observations: Optional[str] = None
    customFields: Optional[Dict[str, Any]] = None
    updatedAt: Optional[datetime] = None

class PatientSchema(BaseModel):
    id: str = Field(min_length=1)
    professionalId: str = Field(min_length=1)
    fullName: str
    motherName: str
    birthDateEpochMillis: int
    phone: Optional[str] = None
    email: Optional[str] = None
    guardianName: Optional[str] = None
    relationship: Optional[str] = None
    notes: Optional[str] = None
    createdAtEpochMillis: int
    updatedAt: Optional[datetime] = None