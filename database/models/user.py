from sqlalchemy import Column, BigInteger, String, Text, DateTime
from sqlalchemy.sql import func
from database.base import Base

class User(Base):
    __tablename__ = "users"

    # Using BigInteger to support Snowflake IDs for massive scale
    id = Column(BigInteger, primary_key=True, index=True)

    # Phone number is the only unique identifier (e.g., +972501234567)
    # Indexed for fast lookups during OTP login
    phone_number = Column(String(20), unique=True, index=True, nullable=False)

    # Display name
    display_name = Column(String(50), nullable=True)

    # Short bio or status text
    about_text = Column(String(150), nullable=True)

    # URL pointing to the image file stored in AWS S3 / MinIO
    profile_pic_url = Column(Text, nullable=True)

    # Audit timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())