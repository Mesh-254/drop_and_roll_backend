# business/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from business.api_views import BusinessInquiryViewSet, BusinessPricingViewSet

router = DefaultRouter()
router.register(r'inquiries', BusinessInquiryViewSet, basename='business-inquiry')
router.register(r'pricings', BusinessPricingViewSet, basename='business-pricing')

urlpatterns = [
    path('', include(router.urls)),
    # Endpoints:
    # /inquiries/ (LIST for admin, CREATE for anyone)
    # /inquiries/{id}/ (RETRIEVE, UPDATE, DELETE for admin)
    # /inquiries/{id}/generate-quote/ (POST for admin)
    # /inquiries/{id}/create-booking/ (POST for admin)
    # /inquiries/{id}/approve-booking/ (POST for admin)
    # /inquiries/{id}/assign-driver/ (POST for admin)
    # /pricings/ (CRUD for admin only)
]