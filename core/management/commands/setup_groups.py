"""
Commande : python manage.py setup_groups

Crée les groupes, les permissions et les utilisateurs initiaux.
À exécuter une seule fois, après 'python manage.py migrate'.

Utilisateurs créés :
  Heni   → superuser (mot de passe affiché en console)
  Tarak  → groupe Administration
  Samir  → groupe Commercial
  Ridha  → groupe Commercial
  Anis   → groupe Production
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand


# Permissions par groupe : (app_label, codename_suffix)
# codename_suffix = 'view' | 'add' | 'change' | 'delete'
GROUP_PERMISSIONS = {
    'Administration': {
        'administration': ['view', 'add', 'change', 'delete'],
        'commercial':     ['view', 'add', 'change', 'delete'],
        'production':     ['view', 'add', 'change', 'delete'],
        'auth':           ['view'],  # accès lecture seule aux users
    },
    'Commercial': {
        'commercial': ['view', 'add', 'change', 'delete'],
        'production': ['view'],  # lecture produits pour les BL
    },
    'Production': {
        'production': ['view', 'add', 'change', 'delete'],
    },
}

# Utilisateurs initiaux : (username, first_name, last_name, groupe, is_superuser)
INITIAL_USERS = [
    ('Heni',  'Heni',  '',       None,             True),
    ('Tarak', 'Tarak', '',       'Administration', False),
    ('Samir', 'Samir', '',       'Commercial',     False),
    ('Ridha', 'Ridha', '',       'Commercial',     False),
    ('Anis',  'Anis',  '',       'Production',     False),
]

# Mot de passe temporaire (à changer après la première connexion)
DEFAULT_PASSWORD = 'Patisserie@2024!'


class Command(BaseCommand):
    help = 'Crée les groupes, permissions et utilisateurs initiaux'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset-passwords',
            action='store_true',
            help='Réinitialise les mots de passe même si les users existent déjà',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Setup Groupes & Utilisateurs ==='))
        self._create_groups()
        self._assign_permissions()
        self._create_users(reset_passwords=options['reset_passwords'])
        self.stdout.write(self.style.SUCCESS('\nSetup terminé avec succès.\n'))
        self.stdout.write(
            self.style.WARNING(
                '⚠  Changez les mots de passe après la première connexion !\n'
            )
        )

    # ─── Groupes ─────────────────────────────────────────────────────────────

    def _create_groups(self):
        self.stdout.write('\n[1/3] Création des groupes...')
        for name in GROUP_PERMISSIONS:
            group, created = Group.objects.get_or_create(name=name)
            status = 'créé' if created else 'existe déjà'
            self.stdout.write(f'  • {name} → {status}')

    # ─── Permissions ─────────────────────────────────────────────────────────

    def _assign_permissions(self):
        self.stdout.write('\n[2/3] Attribution des permissions...')
        for group_name, app_perms in GROUP_PERMISSIONS.items():
            group = Group.objects.get(name=group_name)
            group.permissions.clear()
            count = 0
            for app_label, actions in app_perms.items():
                cts = ContentType.objects.filter(app_label=app_label)
                for ct in cts:
                    for action in actions:
                        codename = f'{action}_{ct.model}'
                        try:
                            perm = Permission.objects.get(codename=codename, content_type=ct)
                            group.permissions.add(perm)
                            count += 1
                        except Permission.DoesNotExist:
                            pass
            self.stdout.write(f'  • {group_name} → {count} permissions assignées')

    # ─── Utilisateurs ─────────────────────────────────────────────────────────

    def _create_users(self, reset_passwords=False):
        User = get_user_model()
        self.stdout.write('\n[3/3] Création des utilisateurs...')
        self.stdout.write(f'  Mot de passe par défaut : {DEFAULT_PASSWORD}')

        for username, first_name, last_name, group_name, is_superuser in INITIAL_USERS:
            user, created = User.objects.get_or_create(username=username)

            if created or reset_passwords:
                user.set_password(DEFAULT_PASSWORD)

            user.first_name = first_name
            user.last_name = last_name
            user.is_superuser = is_superuser
            user.is_staff = is_superuser  # Seul le superuser accède au /admin/
            user.is_active = True
            user.save()

            if group_name:
                group = Group.objects.get(name=group_name)
                user.groups.set([group])  # Un seul groupe par user
            else:
                user.groups.clear()

            status = 'créé' if created else 'mis à jour'
            groupe_label = f'[{group_name}]' if group_name else '[superuser]'
            self.stdout.write(f'  • {username} {groupe_label} → {status}')
