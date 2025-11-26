from __future__ import annotations
from django.contrib.auth.models import (
    AbstractBaseUser,
    PermissionsMixin,
    BaseUserManager,
)
from django.db import models
from django.utils import timezone
from django.core.validators import RegexValidator
import uuid


class UserManager(BaseUserManager):
    """User manager with email as the unique identifier."""

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        if extra_fields.get("is_active") is None:
            user.is_active = True
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("role", User.Role.ADMIN)
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if password is None:
            raise ValueError("Superusers must have a password")
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """Single user table with role and optional phone; UUID PK for microservices safety."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        CUSTOMER = "customer", "Customer"
        DRIVER = "driver", "Driver"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, max_length=255)
    phone = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        validators=[RegexValidator(r"^[0-9+\-()\s]{7,20}$", "Invalid phone number")],
    )
    full_name = models.CharField(max_length=255)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CUSTOMER)

    # Django admin flags
    is_active = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)

    date_joined = models.DateTimeField(default=timezone.now)
    loyalty_points = models.PositiveIntegerField(default=0)  # for customers

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    def get_full_name(self):
        return self.full_name.strip() or self.email.split('@')[0].replace('.', ' ').title()

    def __str__(self):
        return f"{self.get_full_name()} <{self.email}> ({self.role})"

    @property
    def is_admin(self) -> bool:
        return self.role == self.Role.ADMIN

    @property
    def is_driver(self) -> bool:
        return self.role == self.Role.DRIVER

    @property
    def is_customer(self) -> bool:
        return self.role == self.Role.CUSTOMER


class CustomerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="customer_profile")
    default_pickup_address = models.CharField(max_length=255, blank=True, null=True)
    default_dropoff_address = models.CharField(max_length=255, blank=True, null=True)
    preferred_payment_method = models.CharField(max_length=50, blank=True, null=True)

    def __str__(self):
        return f"CustomerProfile({self.user_id})"





class AdminProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="admin_profile")
    department = models.CharField(max_length=100, blank=True, null=True)
    access_level = models.CharField(max_length=50, default="full")

    def __str__(self):
        return f"AdminProfile({self.user_id})"





