from django.contrib.auth import get_user_model
from rest_framework import viewsets, permissions
from drf_yasg.utils import swagger_auto_schema

from .models import CustomerProfile, DriverProfile, AdminProfile
from users.serializers import (
    UserSerializer,
    CustomerProfileSerializer,
    DriverProfileSerializer,
    AdminProfileSerializer,
    ChangePasswordSerializer,
    DriverInviteSerializer,
    DriverInviteAcceptSerializer
)

User = get_user_model()

class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(request_body=ChangePasswordSerializer)
    def change_password(self, request, *args, **kwargs):
        # Change password logic here
        pass

class DriverInviteViewSet(viewsets.ModelViewSet):
    queryset = User.objects.filter(role=User.Role.DRIVER)
    serializer_class = DriverInviteSerializer
    permission_classes = [permissions.IsAdminUser]

    @swagger_auto_schema(request_body=DriverInviteAcceptSerializer)
    def accept_invite(self, request, *args, **kwargs):
        # Accept invite logic here
        pass
