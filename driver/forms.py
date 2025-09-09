from django import forms

from driver.models import DriverProfile


class DriverInvitationForm(forms.Form):
    password = forms.CharField(widget=forms.PasswordInput, label="Password")
    confirm_password = forms.CharField(widget=forms.PasswordInput, label="Confirm Password")
    license_number = forms.CharField(max_length=50, label="License Number")
    vehicle_type = forms.ChoiceField(choices=DriverProfile.Vehicle.choices, label="Vehicle Type")
    vehicle_registration = forms.CharField(max_length=50, required=False, label="Vehicle Registration")

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")
        if password and confirm_password and password != confirm_password:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data
