from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class CarBase(BaseModel):
    title: str = Field(..., max_length=255)
    description: Optional[str] = None
    images: List[str] = Field(default_factory=list)
    thumbnail: Optional[str] = Field(default=None, max_length=500)
    is_active: bool = True


class CarCreate(CarBase):
    pass


class CarUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    images: Optional[List[str]] = None
    thumbnail: Optional[str] = Field(default=None, max_length=500)
    is_active: Optional[bool] = None


class CarOut(CarBase):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ReviewBase(BaseModel):
    author_name: Optional[str] = Field(default=None, max_length=120)
    rating: int = Field(..., ge=1, le=5)
    text: str = Field(..., min_length=2, max_length=2000)


class ReviewCreate(ReviewBase):
    pass


class ReviewOut(ReviewBase):
    id: int
    car_id: int
    is_approved: bool = True
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# --------------------
# Service reviews (about the service)
# --------------------

class ServiceReviewBase(BaseModel):
    author_name: Optional[str] = Field(default=None, max_length=120)
    rating: int = Field(..., ge=1, le=5)
    text: str = Field(..., min_length=2, max_length=2000)


class ServiceReviewCreate(ServiceReviewBase):
    pass


class ServiceReviewOut(ServiceReviewBase):
    id: int
    is_approved: bool = True
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# --------------------
# Services
# --------------------

class ServiceBase(BaseModel):
    name: str = Field(..., max_length=255)
    description: Optional[str] = None
    price_from: Optional[int] = Field(default=None, ge=0)
    is_active: bool = True


class ServiceOut(ServiceBase):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ServicePriceUpdate(BaseModel):
    price_from: int = Field(..., ge=0)


# --------------------
# Vehicle classes
# --------------------

class VehicleClassBase(BaseModel):
    name: str = Field(..., max_length=255)
    description: Optional[str] = None
    price_multiplier: int = Field(default=100, ge=1, le=10000)
    is_active: bool = True


class VehicleClassOut(VehicleClassBase):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class VehicleClassMultiplierUpdate(BaseModel):
    price_multiplier: int = Field(..., ge=1, le=10000)


# --------------------
# Booking requests
# --------------------

class BookingRequestCreate(BaseModel):
    service_id: int
    vehicle_class_id: int
    service_date: datetime
    contact: str = Field(..., min_length=3, max_length=255)
    comment: Optional[str] = Field(default=None, max_length=2000)
    estimated_price: Optional[int] = Field(default=None, ge=0)


class BookingRequestOut(BaseModel):
    id: int
    service_id: int
    vehicle_class_id: int
    service_date: datetime
    contact: str
    comment: Optional[str] = None
    status: str
    estimated_price: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# --------------------
# Price calculation
# --------------------

class BookingPriceCalculateRequest(BaseModel):
    service_id: int
    vehicle_class_id: int


class BookingPriceCalculateResponse(BaseModel):
    service_id: int
    vehicle_class_id: int
    service_name: str
    vehicle_class_name: str
    base_price: int
    price_multiplier: int
    estimated_price: int
    currency: str = "KZT"
    disclaimer: str = "Это примерная стоимость. Итоговая цена зависит от деталей маршрута и условий поездки."


class BookingStatusUpdate(BaseModel):
    status: str = Field(..., pattern=r"^(new|accepted|freelancer|cancelled|completed)$")


class BookingRequestDetail(BookingRequestOut):
    service_name: Optional[str] = None
    vehicle_class_name: Optional[str] = None