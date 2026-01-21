from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(
            u
            and u.is_authenticated
            and (u.is_staff or getattr(u, "role", None) == "admin")
        )


class IsDriver(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and getattr(u, "role", None) == "driver")


class IsCustomer(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and getattr(u, "role", None) == "customer")


class IsDriverOrAdmin(BasePermission):
    """
    Allows drivers to access their own data; admins to access all.
    """

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.user.is_staff:  # Admin check
            return True
        # Driver check: Ensure user has DriverProfile
        return hasattr(request.user, "driver_profile")

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff:
            return True
        # For drivers: Check if obj relates to them
        return obj.driver == request.user.driver_profile  # Adjust based on obj type
