from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import serializers
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

# Assuming this is the correct import
from driver.serializers import DriverProfileSerializer
from .permissions import IsAdmin, IsCustomer, IsDriver
from .serializers import CustomerProfileSerializer, AdminProfileSerializer
from .serializers import (
    UserSerializer,
    RegisterSerializer,
    ChangePasswordSerializer,
    LoginSerializer, ForgotPasswordSerializer, ChangePasswordForgotSerializer,
)
from .tasks import send_confirmation_email, send_reset_email, send_welcome_email

User = get_user_model()


class GoogleLoginView(APIView):
    """
    Handles Google Login by verifying the ID token and logging in or registering the user.
    Compatible with the custom User model defined in models.py.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        # Extract ID token from the request
        id_token_str = request.data.get('token')
        if not id_token_str:
            return Response({
                'code': 'INVALID_TOKEN',
                'error': 'ID token is required'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Verify the ID token using Google's library
            idinfo = id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                settings.GOOGLE_CLIENT_ID
            )
            if idinfo['iss'] not in ['accounts.google.com', 'https://accounts.google.com']:
                raise ValueError('Wrong issuer.')

            # Extract user information from the token
            email = idinfo['email'].lower()
            full_name = f"{idinfo.get('given_name', '')} {idinfo.get('family_name', '')}".strip(
            )

            # Check if user exists; if not, create a new user
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    # Fallback to email username
                    'full_name': full_name or email.split('@')[0],
                    'role': User.Role.CUSTOMER,  # Default to customer role
                    'is_active': True  # Google users are auto-verified
                }
            )

            # If user exists but is not active, activate them
            if not user.is_active:
                user.is_active = True
                user.save()

            # Generate JWT tokens
            refresh = RefreshToken.for_user(user)
            return Response({
                'code': 'AUTH_SUCCESS',
                'refresh': str(refresh),
                'access': str(refresh.access_token),
                'user': UserSerializer(user).data
            }, status=status.HTTP_200_OK)

        except ValueError as e:
            return Response({
                'code': 'INVALID_TOKEN',
                'error': f'Invalid ID token: {str(e)}'
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                'code': 'AUTH_ERROR',
                'error': f'Authentication error: {str(e)}'
            }, status=status.HTTP_400_BAD_REQUEST)


class AuthViewSet(viewsets.GenericViewSet):
    queryset = User.objects.all()
    permission_classes = [AllowAny]

    def get_serializer_class(self):
        if self.action == "register":
            return RegisterSerializer
        elif self.action == "change_password":
            return ChangePasswordSerializer
        elif self.action == "me":
            return UserSerializer
        elif self.action == "login":
            return LoginSerializer
        elif self.action == "forgot_password":
            return ForgotPasswordSerializer  # No input validation needed
        elif self.action == "reset_password":
            return ChangePasswordForgotSerializer  # Reuses new_password field
        return UserSerializer  # default

    @action(methods=["post"], detail=False, url_path="login", permission_classes=[AllowAny])
    def login(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].lower()
        password = serializer.validated_data["password"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({
                "code": "EMAIL_NOT_FOUND",
                "error": "No account found with this email"
            }, status=status.HTTP_400_BAD_REQUEST)

        if not user.is_active:
            return Response({
                "code": "ACCOUNT_NOT_ACTIVATED",
                "error": "Account is not activated"
            }, status=status.HTTP_400_BAD_REQUEST)

        if not user.check_password(password):
            return Response({
                "code": "INVALID_CREDENTIALS",
                "error": "The email and password do not match"
            }, status=status.HTTP_400_BAD_REQUEST)

        refresh = RefreshToken.for_user(user)
        return Response({
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        })

    @action(methods=["post"], detail=False, url_path="register", permission_classes=[AllowAny])
    def register(self, request):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
            user = serializer.save()

            # Send confirmation email
            try:
                token = default_token_generator.make_token(user)
                uid = urlsafe_base64_encode(force_bytes(user.pk))
                subject = "Confirm Your Drop 'N Roll Account"
                confirmation_link = f"{settings.FRONTEND_URL}/account-confirmed/?uid={uid}&token={token}"
                message = (
                    f"Hi {user.full_name},\n\n"
                    f"Please confirm your email by clicking the link below:\n\n"
                    f"{confirmation_link}\n\n"
                    f"If you did not create this account, please ignore this email.\n\n"
                    f"Best,\nDrop 'N Roll Team"
                )
                send_confirmation_email.delay(
                    subject=subject,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email]
                )
            except Exception as e:
                # Log but don't fail registration
                print(f"Failed to send confirmation email: {str(e)}")

            return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)
        except serializers.ValidationError as e:
            # Extract nested field error (e.g., from validate_email) and return flat response
            code = "VALIDATION_ERROR"
            error_msg = "Registration failed"
            if isinstance(e.detail, dict) and len(e.detail) == 1 and "email" in e.detail:
                field_error = e.detail["email"]
                if isinstance(field_error, dict):
                    code = field_error.get("code", "VALIDATION_ERROR")
                    error_msg = field_error.get("error", "Registration failed")
                elif isinstance(field_error, list) and len(field_error) > 0:
                    # Fallback for raw DRF errors (e.g., unique constraint)
                    error_msg = str(field_error[0])
                    if "already exists" in error_msg.lower():
                        code = "ACCOUNT_ALREADY_EXISTS"
            return Response({
                "code": code,
                "error": error_msg
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({
                "code": "REGISTER_ERROR",
                "error": "An unexpected error occurred during registration."
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # function to confirm email address

    @action(methods=["get"], detail=False, url_path="confirm", permission_classes=[AllowAny])
    def confirm(self, request):
        uid = request.query_params.get("uid")
        token = request.query_params.get("token")

        if not uid or not token:
            return Response({
                'code': 'INVALID_CONFIRMATION_LINK',
                'error': 'Missing uid or token in link.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            uid = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({
                "code": "INVALID_CONFIRMATION_LINK",
                "error": "Invalid confirmation link"
            }, status=status.HTTP_400_BAD_REQUEST)

        if user.is_active:
            return Response({
                "code": "ACCOUNT_ALREADY_ACTIVATED",
                "error": "This account is already activated. Please sign in"
            }, status=status.HTTP_400_BAD_REQUEST)

        if default_token_generator.check_token(user, token):
            user.is_active = True
            user.save()
            # NEW: Send welcome email asynchronously after activation
            try:
                subject = "Welcome to Drop 'N Roll!"
                message = (
                    f"Hi {user.full_name},\n\n"
                    f"Congratulations on confirming your account ! We're thrilled to welcome you to the Drop 'N Roll family.\n\n"
                    f"As you embark on this journey with us, we want to assure you that our commitment to your satisfaction is unwavering. "
                    f"From seamless pickups to reliable deliveries, we strive to deliver quality services that not only meet but exceed your expectations. "
                    f"We hope every interaction leaves you with a sense of ease and hope for a smoother, more efficient experience ahead.\n\n"
                    f"Explore our app, place your first order, and let's roll together!\n\n"
                    f"If you have any questions, our support team is here to help.\n\n"
                    f"Warm regards,\n"
                    f"The Drop 'N Roll Team"
                )
                send_welcome_email.delay(
                    subject=subject,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email]
                )
            except Exception as e:
                # Log but don't fail activation—email is non-critical for core flow
                # Replace with logger.error() in prod
                print(f"Failed to send welcome email to {user.email}: {str(e)}")

            return Response({
                'code': 'CONFIRMATION_SUCCESS',
                'detail': 'Email confirmed successfully. You can now sign in.'
            }, status=status.HTTP_200_OK)
        return Response({
            "code": "INVALID_CONFIRMATION_LINK",
            "error": "Invalid or expired confirmation link. Request a new one from resend page."
        }, status=status.HTTP_400_BAD_REQUEST)

    @action(methods=["post"], detail=False, url_path="resend-confirmation", permission_classes=[AllowAny])
    def resend_confirmation(self, request):
        email = request.data.get("email", "").lower()
        if not email:
            return Response({
                "code": "INVALID_EMAIL",
                "error": "Email is required"
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
            if user.is_active:
                return Response({
                    "code": "ACCOUNT_ALREADY_ACTIVATED",
                    "error": "Account is already activated"
                }, status=status.HTTP_400_BAD_REQUEST)
            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(str(user.pk)))
            subject = "Confirm Your Drop 'N Roll Account"
            confirmation_link = f"{settings.FRONTEND_URL}/account-confirmed/?uid={uid}&token={token}"
            message = (
                f"Hi {user.full_name},\n\n"
                f"Please confirm your email by clicking the link below:\n\n"
                f"{confirmation_link}\n\n"
                f"If you did not create this account, please ignore this email.\n\n"
                f"Best,\nDrop 'N Roll Team"
            )
            try:
                send_confirmation_email.delay(
                    subject=subject,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email]
                )
                return Response({
                    "code": "CONFIRMATION_SENT",
                    "detail": "Confirmation email sent"
                }, status=status.HTTP_200_OK)
            except Exception as e:
                return Response({
                    "code": "EMAIL_SEND_FAILED",
                    "error": f"Failed to send confirmation email: {str(e)}"
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except User.DoesNotExist:
            return Response({
                "code": "EMAIL_NOT_FOUND",
                "error": "No account found with this email"
            }, status=status.HTTP_400_BAD_REQUEST)

    # Forgot password logic

    @action(methods=["post"], detail=False, url_path="forgot-password", permission_classes=[AllowAny])
    def forgot_password(self, request):
        email = request.data.get("email", "").lower()
        if not email:
            return Response({
                "code": "INVALID_EMAIL",
                "error": "Email is required"
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
            if not user.is_active:
                return Response({
                    "code": "ACCOUNT_NOT_ACTIVATED",
                    "error": "Account is not activated. Please check your email for confirmation."
                }, status=status.HTTP_400_BAD_REQUEST)

            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(str(user.pk)))
            subject = "Reset Your Drop 'N Roll Password"
            reset_link = f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}"
            message = (
                f"Hi {user.full_name},\n\n"
                f"You requested a password reset. Click the link below to set a new password:\n\n"
                f"{reset_link}\n\n"
                f"If you did not request this, please ignore this email.\n\n"
                f"Best,\nDrop 'N Roll Team"
            )
            try:
                # send_confirmation_email.delay(
                #     subject=subject,
                #     message=message,
                #     from_email=settings.DEFAULT_FROM_EMAIL,
                #     recipient_list=[user.email]
                # )
                send_reset_email.delay(
                    subject=subject,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    # fail_silently=True,
                )
                return Response({
                    "code": "RESET_EMAIL_SENT",
                    "detail": "Password reset email sent. Check your inbox."
                }, status=status.HTTP_200_OK)
            except Exception as e:
                return Response({
                    "code": "EMAIL_SEND_FAILED",
                    "error": f"Failed to send reset email: {str(e)}"
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except User.DoesNotExist:
            return Response({
                "code": "EMAIL_NOT_FOUND",
                "error": "No account found with this email"
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(methods=["post"], detail=False, url_path="reset-password", permission_classes=[AllowAny])
    def reset_password(self, request):
        uid = request.data.get("uid")
        token = request.data.get("token")
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_password = serializer.validated_data["new_password"]

        try:
            uid = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({
                "code": "INVALID_RESET_LINK",
                "error": "Invalid or expired reset link"
            }, status=status.HTTP_400_BAD_REQUEST)

        if default_token_generator.check_token(user, token):
            user.set_password(new_password)
            user.save()
            return Response({
                "code": "PASSWORD_RESET_SUCCESS",
                "detail": "Password reset successfully. You can now log in."
            }, status=status.HTTP_200_OK)
        return Response({
            "code": "INVALID_RESET_LINK",
            "error": "Invalid or expired reset link"
        }, status=status.HTTP_400_BAD_REQUEST)

    @action(
        methods=["get"],
        detail=False,
        url_path="me",
        permission_classes=[IsAuthenticated]
    )
    def me(self, request):
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)

    @action(
        methods=["post"],
        detail=False,
        url_path="change-password",
        permission_classes=[IsAuthenticated]
    )
    def change_password(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not request.user.check_password(serializer.validated_data["old_password"]):
            return Response({
                "code": "INVALID_OLD_PASSWORD",
                "error": "Old password incorrect"
            }, status=status.HTTP_400_BAD_REQUEST)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        return Response({"detail": "Password changed"})


class ProfileViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        """
        Return the appropriate serializer class based on the action.
        """
        serializer_map = {
            'customer': CustomerProfileSerializer,
            'driver': DriverProfileSerializer,
            'admin': AdminProfileSerializer,
        }
        # Default to CustomerProfileSerializer
        return serializer_map.get(self.action, CustomerProfileSerializer)

    @action(methods=["get", "patch"], detail=False, url_path="customer",
            permission_classes=[IsAuthenticated, IsCustomer])
    def customer(self, request):
        profile = request.user.customer_profile
        if request.method == "PATCH":
            s = CustomerProfileSerializer(
                profile, data=request.data, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
        else:
            s = CustomerProfileSerializer(profile)
        return Response(s.data)

    @action(methods=["get", "patch"], detail=False, url_path="driver",
            permission_classes=[IsAuthenticated, IsDriver])
    def driver(self, request):
        profile = request.user.driver_profile
        if request.method == "PATCH":
            s = DriverProfileSerializer(
                profile, data=request.data, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
        else:
            s = DriverProfileSerializer(profile)
        return Response(s.data)

    @action(methods=["get", "patch"], detail=False, url_path="admin",
            permission_classes=[IsAuthenticated, IsAdmin])
    def admin(self, request):
        profile = request.user.admin_profile
        if request.method == "PATCH":
            s = AdminProfileSerializer(
                profile, data=request.data, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
        else:
            s = AdminProfileSerializer(profile)
        return Response(s.data)


class ProfileViewSet1(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]

    @action(methods=["get", "patch"], detail=False, url_path="customer",
            permission_classes=[IsAuthenticated, IsCustomer])
    def customer(self, request):
        profile = request.user.customer_profile
        if request.method == "PATCH":
            s = CustomerProfileSerializer(
                profile, data=request.data, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
        else:
            s = CustomerProfileSerializer(profile)
        return Response(s.data)

    @action(methods=["get", "patch"], detail=False, url_path="driver", permission_classes=[IsAuthenticated, IsDriver])
    def driver(self, request):
        profile = request.user.driver_profile
        if request.method == "PATCH":
            s = DriverProfileSerializer(
                profile, data=request.data, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
        else:
            s = DriverProfileSerializer(profile)
        return Response(s.data)

    @action(methods=["get", "patch"], detail=False, url_path="admin", permission_classes=[IsAuthenticated, IsAdmin])
    def admin(self, request):
        profile = request.user.admin_profile
        if request.method == "PATCH":
            s = AdminProfileSerializer(
                profile, data=request.data, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
        else:
            s = AdminProfileSerializer(profile)
        return Response(s.data)
