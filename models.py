from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    String,
    BigInteger,
    Integer,
    Text,
    JSON,
    DateTime,
    ForeignKey,
    func,
)
from database import Base


class AppSettingsModel(Base):
    __tablename__ = "app_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_app_settings_singleton"),)

    id = Column(Integer, primary_key=True)
    latest_version_code = Column(Integer, nullable=False)
    force_update = Column(Boolean, nullable=False)


class ProfessionalModel(Base):
    __tablename__ = "professionals"
    id = Column(String(255), primary_key=True, index=True)
    firebase_uid = Column(String(255), unique=True, nullable=True)
    plan_type = Column(String(50), default="FREE")
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    subscription_status = Column(String(50), default="TRIAL")
    subscription_expires_at = Column(DateTime(timezone=True), nullable=True)
    purchase_token = Column(String(512), unique=True, index=True, nullable=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    specialty = Column(String(100), nullable=False)
    specialty_id = Column(String(255), nullable=True)
    professional_register = Column(String(100))
    cpf = Column(String(20), unique=True, nullable=False)
    phone = Column(String(50), unique=True)
    signature_url = Column(Text)


class SubscriptionPurchaseTokenModel(Base):
    __tablename__ = "subscription_purchase_tokens"
    purchase_token = Column(String(512), primary_key=True)
    professional_id = Column(
        String(255),
        ForeignKey("professionals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class GooglePlayWebhookEventModel(Base):
    __tablename__ = "google_play_webhook_events"

    event_key = Column(String(64), primary_key=True)
    purchase_token_hash = Column(String(64), nullable=True, index=True)
    notification_type = Column(Integer, nullable=True)
    event_time_millis = Column(String(32), nullable=True)
    status = Column(String(20), nullable=False, index=True)
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    processed_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)


class TrialDeviceModel(Base):
    __tablename__ = "dispositivos_trial"
    android_id = Column(String(255), primary_key=True)
    data_ativacao = Column(DateTime(timezone=True), nullable=False)


class PatientModel(Base):
    __tablename__ = "patients"
    id = Column(String(255), primary_key=True, index=True)
    professional_id = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False)
    mother_name = Column(String(255), nullable=False)
    birth_date_epoch = Column(BigInteger, nullable=False)
    phone = Column(String(50))
    email = Column(String(255))
    guardian_name = Column(String(255))
    relationship = Column(String(100))
    notes = Column(Text)
    created_at_epoch = Column(BigInteger, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

class AppointmentModel(Base):
    __tablename__ = "appointments"
    id = Column(String(255), primary_key=True, index=True)
    professional_id = Column(String(255), nullable=False)
    patient_id = Column(String(255), nullable=False)
    patient_name = Column(String(255), nullable=False)
    recurrence_id = Column(String(255))
    recurrence_rule_id = Column(String(255))
    date_time_epoch = Column(BigInteger, nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    status = Column(String(50), nullable=False)
    notes = Column(Text)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

class EvolutionRecordModel(Base):
    __tablename__ = "evolution_records"
    id = Column(String(255), primary_key=True, index=True)
    appointment_id = Column(String(255))
    patient_id = Column(String(255), nullable=False)
    professional_id = Column(String(255), nullable=False)
    date_epoch = Column(BigInteger, nullable=False)
    activity_performed = Column(Text, nullable=False)
    performance_metrics = Column(Text)
    textual_evolution = Column(Text, nullable=False)
    observations = Column(Text)
    custom_fields = Column(JSON)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )