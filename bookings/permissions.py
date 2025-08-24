from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsCustomer(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and getattr(request.user, "role", None) == "customer"


class IsAdminOrReadOnly(BasePermission):
    """
    SAFE_METHODS (GET, HEAD, OPTIONS) are open.
    Write methods require user to be staff or role='admin'.
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return (
            request.user.is_authenticated
            and (
                request.user.is_staff
                or request.user.is_superuser
                or getattr(request.user, "role", None) == "admin"
            )
        )


class IsOwnerOrAdmin(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.user.is_authenticated and (request.user.is_staff or getattr(request.user, "role", None) == "admin"):
            return True
        return getattr(obj, "customer_id", None) == getattr(request.user, "id", None)
