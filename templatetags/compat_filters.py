from django import template

register = template.Library()

@register.filter
def length_is(value, arg):
    """
    Returns True if the length of the value is the argument.
    Example: {% if some_list|length_is:3 %}
    """
    try:
        return len(value) == int(arg)
    except (ValueError, TypeError):
        return False
