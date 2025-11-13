from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from .models import Ticket, TicketComment, TicketAttachment, TicketType, TicketPriority, TicketStatus
from .tasks import send_ticket_notification  # Import your notification task
from unfold.admin import ModelAdmin, TabularInline


# Inline for comments (allows admins to add/view responses inline)


# Or StackedInline for more space
class TicketCommentInline(TabularInline):
    model = TicketComment
    extra = 1  # Allow adding one new comment by default
    fields = ['user', 'content', 'is_internal', 'created_at']
    readonly_fields = ['user', 'created_at']
    can_delete = False  # Prevent deleting comments for audit purposes

    def has_add_permission(self, request, obj):
        return True  # Admins can always add comments

# Inline for attachments


class TicketAttachmentInline(TabularInline):
    model = TicketAttachment
    extra = 0
    fields = ['file', 'uploaded_by', 'created_at']
    readonly_fields = ['uploaded_by', 'created_at']

# Custom Admin for Ticket


@admin.register(Ticket)
class TicketAdmin(ModelAdmin):
    list_display = ('id', 'subject', 'status', 'priority', 'type',
                    'user_or_guest', 'created_at', 'updated_at', 'assigned_to')
    list_filter = (
        'status',  # Filter by status (e.g., open, closed)
        'priority',  # Filter by priority (e.g., high, medium)
        'type',  # Filter by type/category (e.g., complaint, inquiry)
        # Built-in date filter (by day, week, month, year, or custom range)
        ('created_at', admin.DateFieldListFilter),
        ('updated_at', admin.DateFieldListFilter),  # Same for updated_at
    )
    # Clickable date hierarchy for quick day/month filtering
    date_hierarchy = 'created_at'
    search_fields = ('subject', 'description',
                     'user__username', 'guest_email', 'booking__id')
    readonly_fields = ('created_at', 'updated_at', 'created_by')
    # Inline responses and attachments
    inlines = [TicketCommentInline, TicketAttachmentInline]
    ordering = ('-created_at',)  # Default sort by newest
    list_per_page = 50  # Pagination: 50 items per page (adjust as needed)
    actions = ['mark_as_resolved', 'assign_to_me']  # Custom actions

    def get_queryset(self, request):
        # Default to showing only open tickets (override with filters)
        qs = super().get_queryset(request)
        if not request.GET:  # If no filters applied, default to open
            # Assuming TicketStatus.OPEN from your models
            qs = qs.filter(status=TicketStatus.OPEN)
        return qs

    def user_or_guest(self, obj):
        return obj.user.username if obj.user else obj.guest_email
    user_or_guest.short_description = _('User/Guest')

    # Custom action: Mark as resolved (triggers notification)
    def mark_as_resolved(self, request, queryset):
        # Assuming RESOLVED in TicketStatus
        updated = queryset.update(status=TicketStatus.RESOLVED)
        for ticket in queryset:
            send_ticket_notification.delay(ticket.id, "status_changed")
        self.message_user(request, f"{updated} tickets marked as resolved.")
    mark_as_resolved.short_description = _("Mark selected as resolved")

    # Custom action: Assign to current admin
    def assign_to_me(self, request, queryset):
        updated = queryset.update(assigned_to=request.user)
        for ticket in queryset:
            send_ticket_notification.delay(ticket.id, "updated")
        self.message_user(request, f"{updated} tickets assigned to you.")
    assign_to_me.short_description = _("Assign selected to me")

    # Override save to trigger notifications on changes
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change:  # Only on updates
            action = "status_changed" if 'status' in form.changed_data else "updated"
            send_ticket_notification.delay(obj.id, action)

# Register other models simply


@admin.register(TicketComment)
class TicketCommentAdmin(ModelAdmin):
    list_display = ('id', 'ticket', 'user', 'content_preview',
                    'is_internal', 'created_at')
    list_filter = ('is_internal', 'created_at')
    search_fields = ('content', 'ticket__subject')

    def content_preview(self, obj):
        return obj.content[:50] + '...' if len(obj.content) > 50 else obj.content
    content_preview.short_description = _('Content Preview')


@admin.register(TicketAttachment)
class TicketAttachmentAdmin(ModelAdmin):
    list_display = ('id', 'ticket', 'file', 'uploaded_by', 'created_at')


# @admin.register(TicketType)
# class TicketTypeAdmin(ModelAdmin):
#     list_display = ('name',)


# @admin.register(TicketPriority)
# class TicketPriorityAdmin(ModelAdmin):
#     list_display = ('name',)
