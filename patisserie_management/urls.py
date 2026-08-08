from django.contrib import admin
from django.urls import path, include, register_converter
from django.conf import settings
from django.conf.urls.static import static

from core.converters import HashidConverter

# Enregistré avant les include() des apps : leurs urls.py utilisent <hashid:pk>.
register_converter(HashidConverter, 'hashid')

urlpatterns = [
    path('admin/', admin.site.urls),

    # Core : login / logout / routeur
    path('', include('core.urls', namespace='core')),

    # Apps métier
    path('administration/', include('administration.urls', namespace='administration')),
    path('commercial/',     include('commercial.urls',     namespace='commercial')),
    path('production/',     include('production.urls',     namespace='production')),
    path('administration/sauvegardes/', include('backups.urls', namespace='backups')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
