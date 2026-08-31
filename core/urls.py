from django.contrib.auth.views import LogoutView
from django.urls import path

from .views import CustomLoginView, GroupRouterView, LoginCodeView

app_name = 'core'

urlpatterns = [
    path('', GroupRouterView.as_view(), name='router'),
    path('login/', CustomLoginView.as_view(), name='login'),
    path('login/code/', LoginCodeView.as_view(), name='login-code'),
    path('logout/', LogoutView.as_view(next_page='core:login'), name='logout'),
]
