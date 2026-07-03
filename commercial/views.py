from django.views.generic import TemplateView

from core.mixins import GroupRequiredMixin


class HomeView(GroupRequiredMixin, TemplateView):
    group_required = 'Commercial'
    template_name = 'commercial/home.html'
