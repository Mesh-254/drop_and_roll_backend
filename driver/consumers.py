# driver app  WebSocket consumers for live tracking.

from channels.generic.websocket import AsyncWebsocketConsumer
import json
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed
from asgiref.sync import sync_to_async


class TrackingConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time driver tracking.
    - Admins connect to receive live updates on all active drivers.
    - Uses a single group 'tracking' for broadcasting updates.
    - Authenticates users; only admins (staff) can connect.
    """

    async def connect(self):
        token_str = self.scope["query_string"].decode()
        print(
            f"[TrackingConsumer] Incoming connection with query: {token_str}"
        )  # Debug

        token = token_str.split("token=")[1] if "token=" in token_str else None
        if not token:
            print("[TrackingConsumer] No token provided. Closing.")
            await self.close(code=4003)
            return

        try:
            print("[TrackingConsumer] Validating token...")
            jwt_auth = JWTAuthentication()

            validated_token = jwt_auth.get_validated_token(token)

            get_user_sync = sync_to_async(jwt_auth.get_user)
            user = await get_user_sync(validated_token)

            self.scope["user"] = user
            print(
                f"[TrackingConsumer] User authenticated: {user.email}, is_staff={user.is_staff}"
            )

        except Exception as e:  # Broader catch
            print(f"[TrackingConsumer] Auth failed: {str(e)}")
            await self.close(code=4003)
            return

        # Use role-based check (as we decided earlier)
        if user.is_anonymous or getattr(user, "role", None) != "admin":
            print(
                f"[TrackingConsumer] User not admin (role={getattr(user, 'role', 'None')}) - Rejecting with 4001"
            )
            await self.close(code=4001)
            return

        print("[TrackingConsumer] Adding to group 'tracking'")
        await self.channel_layer.group_add("tracking", self.channel_name)
        print("[TrackingConsumer] Accepting connection")
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("tracking", self.channel_name)

    async def driver_location_update(self, event):
        """
        Handler for 'driver.location.update' events.
        Sends the serialized location data to the connected client.
        """
        await self.send(text_data=json.dumps(event["data"]))

    # receive func to echo pong (keeps connection alive)
    async def receive(self, text_data):
        data = json.loads(text_data)
        if data.get("type") == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
            return


class DriverToggleConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        driver_id = self.scope["url_route"]["kwargs"]["driver_id"]
        print(f"[DriverToggleConsumer] Connecting for driver_id: {driver_id}")
        await self.channel_layer.group_add(f"driver_{driver_id}", self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        driver_id = self.scope["url_route"]["kwargs"]["driver_id"]
        await self.channel_layer.group_discard(f"driver_{driver_id}", self.channel_name)
        print(
            f"[DriverToggleConsumer] Disconnected for driver_id: {driver_id} - code: {close_code}"
        )

    async def tracking_toggle(self, event):
        await self.send(
            text_data=json.dumps(
                {"type": "tracking.toggle", "enabled": event["enabled"]}
            )
        )
