from datetime import datetime
from pydantic import AliasChoices, BaseModel, Field, field_validator
from typing import Optional, Dict, Any
from typing import Literal

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


class ProfessionalResponse(BaseModel):
    id: str
    name: str
    email: str
    specialty: str
    cpf: str
    planType: Optional[str] = None
    subscriptionStatus: Optional[str] = None
    trialEndsAt: Optional[str] = None
    subscriptionExpiresAt: Optional[str] = None
    expirationDate: Optional[str] = None
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
    planType: Literal["MONTHLY", "QUARTERLY", "SEMIANNUAL", "ANNUAL"]


class GooglePubSubMessage(BaseModel):
    data: str = Field(min_length=1)


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
    obfuscatedExternalAccountId: Optional[str] = None


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