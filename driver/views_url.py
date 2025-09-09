from django.urls import path
from driver import views

app_name = "driver"

urlpatterns = [
    path("invitation/<uuid:token>/", views.AcceptDriverInvitationView.as_view(), name="accept_invitation"),
]