from django.contrib.auth import get_user_model
from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.utils.http import urlsafe_base64_decode
from django.utils.encoding import force_str
from django.contrib.auth.tokens import default_token_generator

from .models import DriverDocument
from .permissions import IsAdmin, IsDriver, IsCustomer
from .serializers import (
    UserSerializer,
    RegisterSerializer,
    ChangePasswordSerializer,
    CustomerProfileSerializer,
    DriverProfileSerializer,
    AdminProfileSerializer,
    DriverDocumentSerializer,
    DriverInviteCreateSerializer,
    DriverInviteDetailSerializer,
    DriverInviteAcceptSerializer,
)

User = get_user_model()


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
        return UserSerializer  # default

    @action(methods=["post"], detail=False, url_path="register")
    def register(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)
    

    @action(methods=["get"], detail=False, url_path="confirm", permission_classes=[AllowAny])
    def confirm(self, request):
        uid = request.query_params.get("uid")
        token = request.query_params.get("token")
        try:
            uid = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({"detail": "Invalid confirmation link"}, status=status.HTTP_400_BAD_REQUEST)

        if user.is_active:
            return Response({"detail": "Account is already activated"}, status=status.HTTP_400_BAD_REQUEST)

        if default_token_generator.check_token(user, token):
            user.is_active = True
            user.save()
            return Response({"detail": "Account successfully activated"}, status=status.HTTP_200_OK)
        return Response({"detail": "Invalid confirmation link"}, status=status.HTTP_400_BAD_REQUEST)
    
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
            return Response({"detail": "Old password incorrect"}, status=400)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        return Response({"detail": "Password changed"})
    




class ProfileViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]

    @action(methods=["get", "patch"], detail=False, url_path="customer", permission_classes=[IsAuthenticated, IsCustomer])
    def customer(self, request):
        profile = request.user.customer_profile
        if request.method == "PATCH":
            s = CustomerProfileSerializer(profile, data=request.data, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
        else:
            s = CustomerProfileSerializer(profile)
        return Response(s.data)

    @action(methods=["get", "patch"], detail=False, url_path="driver", permission_classes=[IsAuthenticated, IsDriver])
    def driver(self, request):
        profile = request.user.driver_profile
        if request.method == "PATCH":
            s = DriverProfileSerializer(profile, data=request.data, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
        else:
            s = DriverProfileSerializer(profile)
        return Response(s.data)

    @action(methods=["get", "patch"], detail=False, url_path="admin", permission_classes=[IsAuthenticated, IsAdmin])
    def admin(self, request):
        profile = request.user.admin_profile
        if request.method == "PATCH":
            s = AdminProfileSerializer(profile, data=request.data, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
        else:
            s = AdminProfileSerializer(profile)
        return Response(s.data)


class DriverDocumentViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet
):
    serializer_class = DriverDocumentSerializer
    permission_classes = [IsAuthenticated, IsDriver]

    def get_queryset(self):
        # Prevent schema generation crash in Swagger/Redoc
        if getattr(self, 'swagger_fake_view', False):
            return DriverDocument.objects.none()

        # Only return documents belonging to the logged-in driver
        user = self.request.user
        if hasattr(user, "driver_profile"):
            return user.driver_profile.documents.all()
        return DriverDocument.objects.none()


class DriverInviteViewSet(mixins.CreateModelMixin,
                          mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get_queryset(self):
        from .models import DriverInvitation
        return DriverInvitation.objects.all().order_by("-expires_at")

    def get_serializer_class(self):
        if self.action == "create":
            return DriverInviteCreateSerializer
        return DriverInviteDetailSerializer

    @action(methods=["post"], detail=False, url_path="accept", permission_classes=[AllowAny])
    def accept(self, request):
        s = DriverInviteAcceptSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = s.save()
        return Response(UserSerializer(user).data, status=201)