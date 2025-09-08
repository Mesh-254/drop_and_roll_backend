from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import serializers
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .permissions import IsAdmin, IsDriver, IsCustomer
from .serializers import (
    UserSerializer,
    RegisterSerializer,
    ChangePasswordSerializer,
    CustomerProfileSerializer,

    AdminProfileSerializer,

    LoginSerializer,
)
from .tasks import send_confirmation_email

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
            full_name = f"{idinfo.get('given_name', '')} {idinfo.get('family_name', '')}".strip()

            # Check if user exists; if not, create a new user
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    'full_name': full_name or email.split('@')[0],  # Fallback to email username
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
                # Log the error but don't fail the registration
                print(f"Failed to send confirmation email: {str(e)}")
                # You could add logging here

            return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)
        except serializers.ValidationError as e:
            return Response({
                "code": e.detail.get("code", "UNKNOWN_ERROR"),
                "error": e.detail.get("error", "Registration failed")
            }, status=status.HTTP_400_BAD_REQUEST)

    # function to confirm email address

    @action(methods=["get"], detail=False, url_path="confirm", permission_classes=[AllowAny])
    def confirm(self, request):
        uid = request.query_params.get("uid")
        token = request.query_params.get("token")
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
                "error": "Account is already activated"
            }, status=status.HTTP_400_BAD_REQUEST)

        if default_token_generator.check_token(user, token):
            user.is_active = True
            user.save()
            return Response({
                "code": "ACCOUNT_ACTIVATED",
                "detail": "Account successfully activated"
            }, status=status.HTTP_200_OK)
        return Response({
            "code": "INVALID_CONFIRMATION_LINK",
            "error": "Invalid confirmation link"
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
