from django.contrib import admin
from .models import Ticket, TicketComment, TicketAttachment

class TicketCommentInline(admin.TabularInline):
    model = TicketComment
    extra = 1

class TicketAttachmentInline(admin.TabularInline):
    model = TicketAttachment
    extra = 1

@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("id", "subject", "type", "status", "priority", "user", "booking", "created_at")
    list_filter = ("status", "priority", "type")
    search_fields = ("subject", "description", "user__email", "guest_email")
    inlines = [TicketCommentInline, TicketAttachmentInline]
    actions = ["mark_resolved", "mark_closed"]

    def mark_resolved(self, request, queryset):
        queryset.update(status="resolved")
    mark_resolved.short_description = "Mark selected as Resolved"

    def mark_closed(self, request, queryset):
        queryset.update(status="closed")
    mark_closed.short_description = "Mark selected as Closed"

admin.site.register(TicketComment)
admin.site.register(TicketAttachment)