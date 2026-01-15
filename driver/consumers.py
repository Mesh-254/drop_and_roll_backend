# driver app  WebSocket consumers for live tracking.

from channels.generic.websocket import AsyncWebsocketConsumer
import json


class TrackingConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time driver tracking.
    - Admins connect to receive live updates on all active drivers.
    - Uses a single group 'tracking' for broadcasting updates.
    - Authenticates users; only admins (staff) can connect.
    """

    async def connect(self):
        user = self.scope["user"]
        if user.is_anonymous or not user.is_staff:
            await self.close(code=4001)  # Close with unauthorized code
            return

        await self.channel_layer.group_add("tracking", self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("tracking", self.channel_name)

    async def driver_location_update(self, event):
        """
        Handler for 'driver.location.update' events.
        Sends the serialized location data to the connected client.
        """
        await self.send(text_data=json.dumps(event["data"]))
