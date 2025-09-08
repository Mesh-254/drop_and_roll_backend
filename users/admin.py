
from django.contrib.auth import get_user_model
from django.contrib import admin
from unfold.admin import ModelAdmin

from users.models import CustomerProfile, AdminProfile

User = get_user_model()




@admin.register(User)
class UserAdmin(ModelAdmin):
    list_display = ("email", "full_name", "role", "is_active", "date_joined")
    list_filter = ("role", "is_active", "date_joined")
    search_fields = ("email", "full_name", "phone")
    readonly_fields = ("date_joined",)




@admin.register(CustomerProfile)
class CustomerProfileAdmin(ModelAdmin):
    list_display = ("user", "default_pickup_address", "default_dropoff_address", "preferred_payment_method")


@admin.register(AdminProfile)
class AdminProfileAdmin(ModelAdmin):
    list_display = ("user", "department", "access_level")



