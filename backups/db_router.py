class SauvegardesRouter:
    """Route tous les modèles de l'app "backups" vers la base 'sauvegardes'
    (SQLite, séparée de 'default') — voir le commentaire sur DATABASES dans
    settings.py pour la raison : ces métadonnées ne doivent jamais se
    retrouver dans un dump/restauration de la base métier."""
    route_app_label = 'backups'

    def db_for_read(self, model, **hints):
        if model._meta.app_label == self.route_app_label:
            return 'sauvegardes'
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label == self.route_app_label:
            return 'sauvegardes'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        apps = {obj1._meta.app_label, obj2._meta.app_label}
        if self.route_app_label in apps:
            return apps == {self.route_app_label}
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == self.route_app_label:
            return db == 'sauvegardes'
        if db == 'sauvegardes':
            return False
        return None
