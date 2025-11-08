from rest_framework.permissions import BasePermission
from users.models import User

class IsOwnerOrSupport(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.user.is_authenticated:
            if request.user.role in [User.Role.ADMIN, "support"]:  # Extend roles as needed
                return True
            return obj.user == request.user
        return obj.guest_email == request.query_params.get("guest_email", "").lower()

class IsSupportOrAdmin(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in [User.Role.ADMIN, "support"]