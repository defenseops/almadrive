from __future__ import annotations

import os
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import requests
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload

try:
    from api import models, schemas
    from api.auth import require_admin
    from api.database import get_db
except ImportError:
    import models, schemas  # type: ignore
    from auth import require_admin  # type: ignore
    from database import get_db  # type: ignore


def _env() -> str:
    return (os.getenv("ENVIRONMENT") or "development").strip().lower()


def _is_production() -> bool:
    return _env() in {"prod", "production"}


def _fail_fast() -> None:
    if not (os.getenv("DATABASE_URL") or "").strip():
        raise RuntimeError("DATABASE_URL is required")

    if _is_production():
        if not (os.getenv("ALLOWED_ORIGINS") or "").strip():
            raise RuntimeError("ALLOWED_ORIGINS is required in production")
        if not (os.getenv("ADMIN_USERNAME") or "").strip():
            raise RuntimeError("ADMIN_USERNAME is required in production")
        if not (os.getenv("ADMIN_PASSWORD") or "").strip():
            raise RuntimeError("ADMIN_PASSWORD is required in production")


def get_client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff

    xri = (request.headers.get("x-real-ip") or "").strip()
    if xri:
        return xri

    return (request.client.host if request.client else "").strip()


def get_rate_limit_seconds() -> int:
    raw = (os.getenv("REVIEW_RATE_LIMIT_SECONDS") or "60").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 60

    if value < 1:
        value = 1
    if value > 24 * 3600:
        value = 24 * 3600
    return value


# =========================
# Telegram notifications
# =========================
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

TELEGRAM_ADMINS: List[int] = []
for x in (os.getenv("TELEGRAM_ADMINS") or "").split(","):
    x = x.strip()
    if x.isdigit():
        TELEGRAM_ADMINS.append(int(x))


def send_booking_notification(
    service_name: str,
    vehicle_class_name: str,
    service_date,
    contact: str,
    comment: str | None,
    estimated_price: int | None = None,
    booking_id: int | None = None,
) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMINS:
        print("Telegram booking notification skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_ADMINS not configured")
        return

    if hasattr(service_date, "strftime"):
        service_date_text = service_date.strftime("%d.%m.%Y %H:%M")
    else:
        service_date_text = str(service_date)

    price_text = f"{estimated_price:,} тг".replace(",", " ") if estimated_price is not None else "не рассчитана"
    id_text = f" #{booking_id}" if booking_id is not None else ""

    text = (
        f"🆕 Новая заявка{id_text}\n\n"
        f"🛎 {service_name}\n"
        f"🚘 {vehicle_class_name}\n"
        f"📅 {service_date_text}\n"
        f"💲 Примерная: {price_text}\n"
        f"📞 {contact}\n"
        f"💬 {comment or '—'}"
    )

    reply_markup = None
    if booking_id is not None:
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "🧑 Мы Берём", "callback_data": f"take_booking:{booking_id}"},
                    {"text": "🚗 Наёмник", "callback_data": f"freelancer_booking:{booking_id}"},
                ],
                [{"text": "📋 Открыть заявку", "callback_data": f"open_booking:{booking_id}"}],
            ]
        }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for admin_id in TELEGRAM_ADMINS:
        try:
            payload: dict = {"chat_id": admin_id, "text": text}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            response = requests.post(url, json=payload, timeout=10)
            print(f"Telegram send to {admin_id}: {response.status_code} {response.text}")
        except Exception as e:
            print(f"Failed to send Telegram booking notification to {admin_id}: {e}")


DEFAULT_SERVICES = [
    {
        "name": "Трансфер из аэропорта / в аэропорт",
        "description": "Комфортный трансфер в аэропорт и из аэропорта.",
        "price_from": 8000,
    },
    {
        "name": "Почасовая аренда автомобиля с водителем",
        "description": "Автомобиль с водителем для встреч и поездок по городу.",
        "price_from": 7000,
    },
    {
        "name": "Междугородние поездки по Казахстану",
        "description": "Поездки между городами с индивидуальным расчетом маршрута.",
        "price_from": 10000,
    },
    {
        "name": "Транспортное обслуживание мероприятий",
        "description": "Перевозка гостей для конференций, свадеб и корпоративных событий.",
        "price_from": 10000,
    },
    {
        "name": "Обслуживание делегаций",
        "description": "Транспортное сопровождение деловых и официальных делегаций.",
        "price_from": 12000,
    },
    {
        "name": "Туристические поездки и экскурсии",
        "description": "Индивидуальные туристические маршруты и экскурсии.",
        "price_from": 10000,
    },
]

DEFAULT_VEHICLE_CLASSES = [
    {
        "name": "Бизнес-класс",
        "description": "Комфортные автомобили для деловых поездок.",
        "price_multiplier": 100,
    },
    {
        "name": "Премиум-класс",
        "description": "Премиальные автомобили для особых поездок.",
        "price_multiplier": 150,
    },
    {
        "name": "Минивэн",
        "description": "Вместительные автомобили для групп и багажа.",
        "price_multiplier": 130,
    },
]


def ensure_reference_data(db: Session) -> None:
    has_services = db.query(models.Service.id).first() is not None
    if not has_services:
        for item in DEFAULT_SERVICES:
            db.add(models.Service(**item, is_active=True))

    has_vehicle_classes = db.query(models.VehicleClass.id).first() is not None
    if not has_vehicle_classes:
        for item in DEFAULT_VEHICLE_CLASSES:
            db.add(models.VehicleClass(**item, is_active=True))

    if (not has_services) or (not has_vehicle_classes):
        db.commit()


def bootstrap_database() -> None:
    try:
        from api.database import create_tables, SessionLocal
    except ImportError:
        from database import create_tables, SessionLocal  # type: ignore

    create_tables()
    if SessionLocal is None:
        return

    db = SessionLocal()
    try:
        ensure_reference_data(db)
    finally:
        db.close()


def calculate_estimated_price(service: models.Service, vehicle_class: models.VehicleClass) -> int:
    base_price = service.price_from or 0
    multiplier = vehicle_class.price_multiplier or 100
    estimated = int((base_price * multiplier) / 100)
    return max(estimated, 0)


_fail_fast()

enable_docs = (os.getenv("ENABLE_DOCS") or "").strip() == "1"
docs_url = "/docs" if (not _is_production() or enable_docs) else None
redoc_url = "/redoc" if (not _is_production() or enable_docs) else None
openapi_url = "/openapi.json" if (not _is_production() or enable_docs) else None

app = FastAPI(title="AlmaDrive API", docs_url=docs_url, redoc_url=redoc_url, openapi_url=openapi_url)
BASE_DIR = Path(__file__).resolve().parent.parent

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/js", StaticFiles(directory=str(BASE_DIR / "js")), name="js")
app.mount("/locales", StaticFiles(directory=str(BASE_DIR / "locales")), name="locales")
app.mount("/styles", StaticFiles(directory=str(BASE_DIR / "styles")), name="styles")


@app.get("/sw.js")
def service_worker():
    return FileResponse(str(BASE_DIR / "sw.js"))


allowed = (os.getenv("ALLOWED_ORIGINS") or "*").split(",")
allowed = [x.strip() for x in allowed if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed if allowed else ["*"],
    allow_credentials=False if "*" in allowed else True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent.parent
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
def startup_event() -> None:
    bootstrap_database()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "env": _env()}


# --------------------
# Booking reference data + requests
# --------------------
@app.get("/api/services", response_model=List[schemas.ServiceOut])
def list_services(db: Session = Depends(get_db)):
    ensure_reference_data(db)
    return (
        db.query(models.Service)
        .filter(models.Service.is_active.is_(True))
        .order_by(models.Service.id.asc())
        .all()
    )


@app.get("/api/vehicle-classes", response_model=List[schemas.VehicleClassOut])
def list_vehicle_classes(db: Session = Depends(get_db)):
    ensure_reference_data(db)
    return (
        db.query(models.VehicleClass)
        .filter(models.VehicleClass.is_active.is_(True))
        .order_by(models.VehicleClass.id.asc())
        .all()
    )


@app.post("/api/calculate-booking-price", response_model=schemas.BookingPriceCalculateResponse)
def calculate_booking_price(payload: schemas.BookingPriceCalculateRequest, db: Session = Depends(get_db)):
    ensure_reference_data(db)

    service = db.query(models.Service).filter(models.Service.id == payload.service_id).first()
    if not service or not service.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    vehicle_class = db.query(models.VehicleClass).filter(models.VehicleClass.id == payload.vehicle_class_id).first()
    if not vehicle_class or not vehicle_class.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle class not found")

    base_price = service.price_from or 0
    multiplier = vehicle_class.price_multiplier or 100
    estimated_price = calculate_estimated_price(service, vehicle_class)

    return schemas.BookingPriceCalculateResponse(
        service_id=service.id,
        vehicle_class_id=vehicle_class.id,
        service_name=service.name,
        vehicle_class_name=vehicle_class.name,
        base_price=base_price,
        price_multiplier=multiplier,
        estimated_price=estimated_price,
        currency="KZT",
        disclaimer="Это примерная стоимость. Итоговая цена зависит от деталей маршрута и условий поездки.",
    )


@app.post("/api/booking-requests", response_model=schemas.BookingRequestOut, status_code=status.HTTP_201_CREATED)
def create_booking_request(payload: schemas.BookingRequestCreate, db: Session = Depends(get_db)):
    ensure_reference_data(db)

    service = db.query(models.Service).filter(models.Service.id == payload.service_id).first()
    if not service or not service.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    vehicle_class = db.query(models.VehicleClass).filter(models.VehicleClass.id == payload.vehicle_class_id).first()
    if not vehicle_class or not vehicle_class.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle class not found")

    estimated_price = payload.estimated_price
    if estimated_price is None:
        estimated_price = calculate_estimated_price(service, vehicle_class)

    booking = models.BookingRequest(
        service_id=payload.service_id,
        vehicle_class_id=payload.vehicle_class_id,
        service_date=payload.service_date,
        contact=payload.contact.strip(),
        comment=(payload.comment or "").strip() or None,
        status="new",
        estimated_price=estimated_price,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    try:
        send_booking_notification(
            service_name=service.name,
            vehicle_class_name=vehicle_class.name,
            service_date=booking.service_date,
            contact=booking.contact,
            comment=booking.comment,
            estimated_price=booking.estimated_price,
            booking_id=booking.id,
        )
    except Exception as e:
        print(f"Booking saved, but Telegram notification failed: {e}")

    return booking


# --------------------
# Tariffs admin API
# --------------------
@app.get("/api/admin/services", response_model=List[schemas.ServiceOut])
def admin_list_services(
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ensure_reference_data(db)
    return db.query(models.Service).order_by(models.Service.id.asc()).all()


@app.put("/api/admin/services/{service_id}/price", response_model=schemas.ServiceOut)
def update_service_price(
    service_id: int,
    payload: schemas.ServicePriceUpdate,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    service = db.query(models.Service).filter(models.Service.id == service_id).first()
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    service.price_from = payload.price_from
    db.commit()
    db.refresh(service)
    return service


@app.get("/api/admin/vehicle-classes", response_model=List[schemas.VehicleClassOut])
def admin_list_vehicle_classes(
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ensure_reference_data(db)
    return db.query(models.VehicleClass).order_by(models.VehicleClass.id.asc()).all()


@app.put("/api/admin/vehicle-classes/{class_id}/multiplier", response_model=schemas.VehicleClassOut)
def update_vehicle_class_multiplier(
    class_id: int,
    payload: schemas.VehicleClassMultiplierUpdate,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    vehicle_class = db.query(models.VehicleClass).filter(models.VehicleClass.id == class_id).first()
    if not vehicle_class:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle class not found")

    vehicle_class.price_multiplier = payload.price_multiplier
    db.commit()
    db.refresh(vehicle_class)
    return vehicle_class


# --------------------
# Cars (public read)
# --------------------
@app.get("/api/cars", response_model=List[schemas.CarOut])
def list_cars(
    q: Optional[str] = Query(default=None, description="Search in title/description"),
    active_only: bool = Query(default=True, description="Return only active cars"),
    db: Session = Depends(get_db),
):
    query = db.query(models.Car)

    if active_only:
        query = query.filter(models.Car.is_active.is_(True))

    if q:
        like = f"%{q.strip()}%"
        query = query.filter((models.Car.title.ilike(like)) | (models.Car.description.ilike(like)))

    return query.order_by(models.Car.id.desc()).all()


@app.get("/api/cars/{car_id}", response_model=schemas.CarOut)
def get_car(car_id: int, db: Session = Depends(get_db)):
    car = db.query(models.Car).filter(models.Car.id == car_id).first()
    if not car:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Car not found")
    return car


# --------------------
# Reviews (public)
# --------------------
@app.get("/api/cars/{car_id}/reviews", response_model=List[schemas.ReviewOut])
def list_reviews(
    car_id: int,
    approved_only: bool = Query(default=True, description="Return only approved reviews"),
    db: Session = Depends(get_db),
):
    q = db.query(models.Review).filter(models.Review.car_id == car_id)
    if approved_only:
        q = q.filter(models.Review.is_approved.is_(True))
    return q.order_by(models.Review.id.desc()).all()


@app.post("/api/cars/{car_id}/reviews", response_model=schemas.ReviewOut, status_code=status.HTTP_201_CREATED)
def create_review(
    car_id: int,
    payload: schemas.ReviewCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    car = db.query(models.Car).filter(models.Car.id == car_id).first()
    if not car:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Car not found")

    client_ip = get_client_ip(request)
    salt = (os.getenv("REVIEW_SALT") or "almadrive").strip()
    ip_hash = hashlib.sha256(f"{client_ip}|{salt}".encode("utf-8")).hexdigest() if client_ip else None

    if ip_hash:
        limit_seconds = get_rate_limit_seconds()
        since = datetime.utcnow() - timedelta(seconds=limit_seconds)
        recent = (
            db.query(models.Review)
            .filter(models.Review.car_id == car_id)
            .filter(models.Review.ip_hash == ip_hash)
            .filter(models.Review.created_at >= since)
            .first()
        )
        if recent:
            try:
                elapsed = (datetime.utcnow() - recent.created_at).total_seconds()
                wait = max(0, int(limit_seconds - elapsed))
            except Exception:
                wait = limit_seconds

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many reviews. Please try again in {wait} seconds.",
            )

    review = models.Review(
        car_id=car_id,
        author_name=(payload.author_name or None),
        rating=payload.rating,
        text=payload.text.strip(),
        is_approved=False,
        ip_hash=ip_hash,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


# --------------------
# Reviews (admin)
# --------------------
@app.get("/api/reviews/pending", response_model=List[schemas.ReviewOut])
def list_pending_reviews(
    limit: int = Query(default=20, ge=1, le=100),
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Review)
        .filter(models.Review.is_approved.is_(False))
        .order_by(models.Review.id.desc())
        .limit(limit)
        .all()
    )


@app.put("/api/reviews/{review_id}/approve", response_model=schemas.ReviewOut)
def approve_review(
    review_id: int,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    review = db.query(models.Review).filter(models.Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    review.is_approved = True
    db.commit()
    db.refresh(review)
    return review


@app.put("/api/reviews/{review_id}/hide", response_model=schemas.ReviewOut)
def hide_review(
    review_id: int,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    review = db.query(models.Review).filter(models.Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    review.is_approved = False
    db.commit()
    db.refresh(review)
    return review


@app.delete("/api/reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_review(
    review_id: int,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    review = db.query(models.Review).filter(models.Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    db.delete(review)
    db.commit()
    return None


# --------------------
# Service reviews (public)
# --------------------
@app.get("/api/service-reviews", response_model=List[schemas.ServiceReviewOut])
def list_service_reviews(
    approved_only: bool = Query(default=True),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    q = db.query(models.ServiceReview)
    if approved_only:
        q = q.filter(models.ServiceReview.is_approved.is_(True))
    return q.order_by(models.ServiceReview.id.desc()).limit(limit).all()


@app.post("/api/service-reviews", response_model=schemas.ServiceReviewOut, status_code=status.HTTP_201_CREATED)
def create_service_review(
    payload: schemas.ServiceReviewCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    client_ip = get_client_ip(request)
    salt = (os.getenv("REVIEW_SALT") or "almadrive").strip()
    ip_hash = hashlib.sha256(f"{client_ip}|{salt}".encode("utf-8")).hexdigest() if client_ip else None

    if ip_hash:
        limit_seconds = get_rate_limit_seconds()
        since = datetime.utcnow() - timedelta(seconds=limit_seconds)
        recent = (
            db.query(models.ServiceReview)
            .filter(models.ServiceReview.ip_hash == ip_hash)
            .filter(models.ServiceReview.created_at >= since)
            .first()
        )
        if recent:
            try:
                elapsed = (datetime.utcnow() - recent.created_at).total_seconds()
                wait = max(0, int(limit_seconds - elapsed))
            except Exception:
                wait = limit_seconds

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many reviews. Please try again in {wait} seconds.",
            )

    review = models.ServiceReview(
        author_name=(payload.author_name or None),
        rating=payload.rating,
        text=payload.text.strip(),
        is_approved=False,
        ip_hash=ip_hash,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


# --------------------
# Service reviews (admin)
# --------------------
@app.get("/api/service-reviews/pending", response_model=List[schemas.ServiceReviewOut])
def list_pending_service_reviews(
    limit: int = Query(default=20, ge=1, le=100),
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.ServiceReview)
        .filter(models.ServiceReview.is_approved.is_(False))
        .order_by(models.ServiceReview.id.desc())
        .limit(limit)
        .all()
    )


@app.put("/api/service-reviews/{review_id}/approve", response_model=schemas.ServiceReviewOut)
def approve_service_review(
    review_id: int,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    review = db.query(models.ServiceReview).filter(models.ServiceReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    review.is_approved = True
    db.commit()
    db.refresh(review)
    return review


@app.put("/api/service-reviews/{review_id}/hide", response_model=schemas.ServiceReviewOut)
def hide_service_review(
    review_id: int,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    review = db.query(models.ServiceReview).filter(models.ServiceReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    review.is_approved = False
    db.commit()
    db.refresh(review)
    return review


@app.delete("/api/service-reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service_review(
    review_id: int,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    review = db.query(models.ServiceReview).filter(models.ServiceReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    db.delete(review)
    db.commit()
    return None


# --------------------
# Booking requests (admin)
# --------------------
@app.get("/api/admin/booking-requests", response_model=List[schemas.BookingRequestDetail])
def admin_list_booking_requests(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(models.BookingRequest).options(
        joinedload(models.BookingRequest.service),
        joinedload(models.BookingRequest.vehicle_class),
    )
    if status:
        q = q.filter(models.BookingRequest.status == status)
    bookings = q.order_by(models.BookingRequest.id.desc()).limit(limit).all()
    result = []
    for b in bookings:
        item = schemas.BookingRequestDetail.model_validate(b)
        item.service_name = b.service.name if b.service else None
        item.vehicle_class_name = b.vehicle_class.name if b.vehicle_class else None
        result.append(item)
    return result


@app.get("/api/admin/booking-requests/{booking_id}", response_model=schemas.BookingRequestDetail)
def admin_get_booking_request(
    booking_id: int,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    b = (
        db.query(models.BookingRequest)
        .options(
            joinedload(models.BookingRequest.service),
            joinedload(models.BookingRequest.vehicle_class),
        )
        .filter(models.BookingRequest.id == booking_id)
        .first()
    )
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    item = schemas.BookingRequestDetail.model_validate(b)
    item.service_name = b.service.name if b.service else None
    item.vehicle_class_name = b.vehicle_class.name if b.vehicle_class else None
    return item


@app.put("/api/admin/booking-requests/{booking_id}/status", response_model=schemas.BookingRequestOut)
def update_booking_status(
    booking_id: int,
    payload: schemas.BookingStatusUpdate,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    b = db.query(models.BookingRequest).filter(models.BookingRequest.id == booking_id).first()
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    b.status = payload.status
    db.commit()
    db.refresh(b)
    return b


# --------------------
# Cars (admin write)
# --------------------
@app.post("/api/cars", response_model=schemas.CarOut, status_code=status.HTTP_201_CREATED)
def create_car(
    payload: schemas.CarCreate,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    car = models.Car(
        title=payload.title.strip(),
        description=(payload.description or None),
        images=list(payload.images or []),
        thumbnail=(payload.thumbnail or None),
        is_active=bool(payload.is_active),
    )
    db.add(car)
    db.commit()
    db.refresh(car)
    return car


@app.put("/api/cars/{car_id}", response_model=schemas.CarOut)
def update_car(
    car_id: int,
    payload: schemas.CarUpdate,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    car = db.query(models.Car).filter(models.Car.id == car_id).first()
    if not car:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Car not found")

    data = payload.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        car.title = data["title"].strip()
    if "description" in data:
        car.description = data["description"]
    if "images" in data and data["images"] is not None:
        car.images = list(data["images"])
    if "thumbnail" in data:
        car.thumbnail = data["thumbnail"]
    if "is_active" in data and data["is_active"] is not None:
        car.is_active = bool(data["is_active"])

    db.commit()
    db.refresh(car)
    return car


@app.delete("/api/cars/{car_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_car(
    car_id: int,
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    car = db.query(models.Car).filter(models.Car.id == car_id).first()
    if not car:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Car not found")
    db.delete(car)
    db.commit()
    return None


@app.get("/", include_in_schema=False)
def root():
    index_path = BASE_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"status": "ok"}

BASE_DIR = Path(__file__).resolve().parent.parent


@app.get("/airport-transfer-almaty.html", include_in_schema=False)
def serve_airport_transfer():
    return FileResponse(BASE_DIR / "airport-transfer-almaty.html")


@app.get("/chauffeur-service-almaty.html", include_in_schema=False)
def serve_chauffeur_service():
    return FileResponse(BASE_DIR / "chauffeur-service-almaty.html")


@app.get("/vip-transfer-almaty.html", include_in_schema=False)
def serve_vip_transfer():
    return FileResponse(BASE_DIR / "vip-transfer-almaty.html")


@app.get("/robots.txt", include_in_schema=False)
def serve_robots():
    return FileResponse(BASE_DIR / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def serve_sitemap():
    return FileResponse(BASE_DIR / "sitemap.xml", media_type="application/xml")