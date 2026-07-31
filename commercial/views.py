import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max, Q, RestrictedError, Sum
from django.forms.models import model_to_dict
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.generic import TemplateView, View
from xhtml2pdf import pisa

from administration.models import LibelleTransaction, Transaction
from core.audit import AuditAction, log_audit
from core.mixins import GroupRequiredMixin
from production.models import (
    BonRestitution, BonRestitutionDetaille, BonSortie, BonSortieDetaille, MatierePremiere, Produit,
)

from .forms import ClientForm, FournisseurForm
from .models import (
    Achat, AchatCode, AchatDetaille, Agenda, BonLivraison, BonLivraisonCode, BonLivraisonDetaille,
    Client, ClientUser, Delegation, Depense, DepenseCode, DepenseDetaille, Fournisseur, Gouvernorat,
    HistoriqueStockInitial, HistoriqueStockInitialDetaille, Zone,
    MODE_PAIEMENT_CHOICES, STATUT_PAIEMENT_CHOICES,
)

User = get_user_model()

FOURNISSEUR_TYPE_ACHAT = 'Matières premières et Emballages'
LIBELLE_PAIEMENT_ACHAT = 'Paiement nouveau achat matière première'
LIBELLE_PAIEMENT_ANCIEN_ACHAT = 'Paiement ancien achat matière première'


class HomeView(GroupRequiredMixin, TemplateView):
    group_required = 'Commercial'
    template_name = 'commercial/home.html'


def _form_errors_text(form):
    return ' '.join(e for errors in form.errors.values() for e in errors) or 'Données invalides.'


def _is_administration(user):
    return user.is_superuser or user.groups.filter(name='Administration').exists()


def _is_commercial(user):
    return user.groups.filter(name='Commercial').exists()


def _client_queryset_for_user(user):
    if _is_administration(user):
        return Client.objects.all()
    return Client.objects.filter(client_users__user=user)


# ─── Clients ─────────────────────────────────────────────────────────────────

class ClientListView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/client/list.html'
    PAGINATE_BY = 8

    def get(self, request):
        q = request.GET.get('q', '').strip()

        qs = _client_queryset_for_user(request.user).select_related(
            'zone__delegation__gouvernorat',
        ).distinct()
        if q:
            qs = qs.filter(
                Q(raison_sociale__icontains=q) |
                Q(nom_client__icontains=q) |
                Q(telephone__icontains=q)
            )
        qs = qs.order_by('zone__nom', 'raison_sociale')

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':     page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'clients':      page_obj.object_list,
            'q':            q,
        })


class ClientCreateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/client/create.html'

    def _context(self, request, **overrides):
        delegations_list = Delegation.objects.select_related('gouvernorat').order_by(
            'gouvernorat__nom', 'nom_delegation',
        )
        zones_list = Zone.objects.select_related('delegation').order_by(
            'delegation__nom_delegation', 'nom',
        )
        commerciaux_list = (
            User.objects.filter(groups__name='Commercial')
            .exclude(pk=request.user.pk)
            .order_by('last_name', 'first_name')
        )
        is_admin = _is_administration(request.user)

        form = overrides.pop('form', None) or ClientForm()
        if not is_admin:
            form.fields['is_active'].widget.attrs['disabled'] = 'disabled'

        context = {
            'form': form,
            'is_administration': is_admin,
            'gouvernorats_list': Gouvernorat.objects.order_by('nom'),
            'delegations_list': delegations_list,
            'delegations_json': json.dumps([
                {'id': d.pk, 'nom': d.nom_delegation, 'gouvernorat_id': d.gouvernorat_id}
                for d in delegations_list
            ]),
            'zones_json': json.dumps([
                {'id': z.pk, 'nom': z.nom, 'delegation_id': z.delegation_id}
                for z in zones_list
            ]),
            'commerciaux_json': json.dumps([
                {'id': u.pk, 'label': u.get_full_name() or u.username}
                for u in commerciaux_list
            ]),
            'selected_gouvernorat_id': '',
            'selected_delegation_id': '',
            'zone_nom_value': '',
            'selected_commercial_ids_json': '[]',
        }
        context.update(overrides)
        return context

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        is_admin = _is_administration(request.user)

        raison_sociale = request.POST.get('raison_sociale', '').strip()
        nom_client = request.POST.get('nom_client', '').strip()
        adresse = request.POST.get('adresse', '').strip()
        telephone = request.POST.get('telephone', '').strip()
        google_mape = request.POST.get('google_mape', '').strip()
        observation = request.POST.get('observation', '').strip()
        a_visiter_apres_s = request.POST.get('a_visiter_apres', '').strip()
        created_at_s = request.POST.get('created_at', '').strip()
        is_active = ('is_active' in request.POST) if is_admin else True
        photo = request.FILES.get('photo')

        delegation_id = request.POST.get('delegation', '').strip()
        zone_nom = request.POST.get('zone_nom', '').strip()
        commercial_ids = list(dict.fromkeys(
            v.strip() for v in request.POST.getlist('commercial_id') if v.strip()
        ))

        delegation_obj = Delegation.objects.filter(pk=delegation_id).first() if delegation_id else None
        gouvernorat_id_preserve = str(delegation_obj.gouvernorat_id) if delegation_obj else ''

        def _echec(message):
            messages.error(request, message)
            form = ClientForm(initial={
                'raison_sociale': raison_sociale,
                'nom_client': nom_client,
                'adresse': adresse,
                'telephone': telephone,
                'google_mape': google_mape,
                'observation': observation,
                'a_visiter_apres': a_visiter_apres_s or None,
                'created_at': created_at_s or None,
                'is_active': is_active,
            })
            return render(request, self.template_name, self._context(
                request, form=form,
                selected_gouvernorat_id=gouvernorat_id_preserve,
                selected_delegation_id=delegation_id,
                zone_nom_value=zone_nom,
                selected_commercial_ids_json=json.dumps(commercial_ids),
            ))

        # ── Validations ─────────────────────────────────────────────────────
        if not raison_sociale:
            return _echec('La raison sociale est obligatoire.')
        if not zone_nom:
            return _echec('La zone est obligatoire.')
        if Client.objects.filter(raison_sociale=raison_sociale).exists():
            return _echec(f'Un client avec la raison sociale « {raison_sociale} » existe déjà.')
        if is_admin and not commercial_ids:
            return _echec(
                'Veuillez sélectionner au moins un commercial dans la section '
                '« Ajout autres Commerciaux ».'
            )

        zone_existante = Zone.objects.filter(nom=zone_nom).first()
        if zone_existante is None and not delegation_id:
            return _echec('La délégation est obligatoire pour créer une nouvelle zone.')
        if zone_existante is None and delegation_obj is None:
            return _echec('La délégation sélectionnée est introuvable.')

        a_visiter_apres = None
        if a_visiter_apres_s:
            try:
                a_visiter_apres = int(a_visiter_apres_s)
            except ValueError:
                return _echec('Le champ « à visiter après » doit être un nombre entier.')

        created_at_val = None
        if created_at_s:
            try:
                created_at_val = date.fromisoformat(created_at_s)
            except ValueError:
                return _echec('La date de création est invalide.')

        is_commercial = request.user.groups.filter(name='Commercial').exists()

        try:
            with transaction.atomic():
                zone_obj = zone_existante or Zone.objects.create(delegation=delegation_obj, nom=zone_nom)

                client = Client.objects.create(
                    raison_sociale=raison_sociale,
                    nom_client=nom_client or None,
                    adresse=adresse or None,
                    zone=zone_obj,
                    telephone=telephone or None,
                    photo=photo,
                    google_mape=google_mape or None,
                    a_visiter_apres=a_visiter_apres,
                    observation=observation or None,
                    is_active=is_active,
                    created_at=created_at_val,
                )

                if is_admin:
                    ClientUser.objects.bulk_create([
                        ClientUser(client=client, user_id=uid) for uid in commercial_ids
                    ])
                elif is_commercial:
                    ClientUser.objects.create(client=client, user=request.user)
        except Exception:
            messages.error(
                request,
                "Échec de l'enregistrement du client : aucune donnée n'a été modifiée.",
            )
            return render(request, self.template_name, self._context(
                request,
                selected_gouvernorat_id=gouvernorat_id_preserve,
                selected_delegation_id=delegation_id,
                zone_nom_value=zone_nom,
                selected_commercial_ids_json=json.dumps(commercial_ids),
            ))

        log_audit(
            AuditAction.CREATE, f'Création client « {client} »',
            table='Client', record_id=client.pk, new_value=model_to_dict(client),
        )
        messages.success(request, f'Client « {client} » enregistré avec succès.')
        return redirect('commercial:client-list')


class ClientDetailView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/client/detail.html'

    def get(self, request, pk):
        client = get_object_or_404(_client_queryset_for_user(request.user), pk=pk)
        commerciaux = client.client_users.select_related('user').order_by(
            'user__last_name', 'user__first_name',
        )
        return render(request, self.template_name, {'client': client, 'commerciaux': commerciaux})


class ClientUpdateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/client/update.html'

    def _context(self, request, client, **overrides):
        delegations_list = Delegation.objects.select_related('gouvernorat').order_by(
            'gouvernorat__nom', 'nom_delegation',
        )
        zones_list = Zone.objects.select_related('delegation').order_by(
            'delegation__nom_delegation', 'nom',
        )
        commerciaux_list = (
            User.objects.filter(groups__name='Commercial')
            .exclude(pk=request.user.pk)
            .order_by('last_name', 'first_name')
        )
        is_admin = _is_administration(request.user)

        form = overrides.pop('form', None) or ClientForm(instance=client)

        context = {
            'client': client,
            'form': form,
            'is_administration': is_admin,
            'gouvernorats_list': Gouvernorat.objects.order_by('nom'),
            'delegations_list': delegations_list,
            'delegations_json': json.dumps([
                {'id': d.pk, 'nom': d.nom_delegation, 'gouvernorat_id': d.gouvernorat_id}
                for d in delegations_list
            ]),
            'zones_json': json.dumps([
                {'id': z.pk, 'nom': z.nom, 'delegation_id': z.delegation_id}
                for z in zones_list
            ]),
            'commerciaux_json': json.dumps([
                {'id': u.pk, 'label': u.get_full_name() or u.username}
                for u in commerciaux_list
            ]),
            'selected_gouvernorat_id': str(client.zone.delegation.gouvernorat_id) if client.zone_id else '',
            'selected_delegation_id': str(client.zone.delegation_id) if client.zone_id else '',
            'zone_nom_value': client.zone.nom if client.zone_id else '',
            'selected_commercial_ids_json': json.dumps([
                str(uid) for uid in client.client_users.values_list('user_id', flat=True)
            ]),
        }
        context.update(overrides)
        return context

    def get(self, request, pk):
        client = get_object_or_404(
            _client_queryset_for_user(request.user).select_related('zone__delegation__gouvernorat'), pk=pk,
        )
        return render(request, self.template_name, self._context(request, client))

    def post(self, request, pk):
        client = get_object_or_404(
            _client_queryset_for_user(request.user).select_related('zone__delegation__gouvernorat'), pk=pk,
        )
        is_admin = _is_administration(request.user)

        delegation_id = request.POST.get('delegation', '').strip()
        zone_nom = request.POST.get('zone_nom', '').strip()
        commercial_ids = list(dict.fromkeys(
            v.strip() for v in request.POST.getlist('commercial_id') if v.strip()
        ))

        delegation_obj = Delegation.objects.filter(pk=delegation_id).first() if delegation_id else None
        gouvernorat_id_preserve = str(delegation_obj.gouvernorat_id) if delegation_obj else ''

        avant = model_to_dict(client)
        form = ClientForm(request.POST, request.FILES, instance=client)

        def _echec(message):
            messages.error(request, message)
            return render(request, self.template_name, self._context(
                request, client, form=form,
                selected_gouvernorat_id=gouvernorat_id_preserve,
                selected_delegation_id=delegation_id,
                zone_nom_value=zone_nom,
                selected_commercial_ids_json=json.dumps(commercial_ids),
            ))

        if not form.is_valid():
            return _echec(_form_errors_text(form))
        if not zone_nom:
            return _echec('La zone est obligatoire.')
        if is_admin and not commercial_ids:
            return _echec(
                'Veuillez sélectionner au moins un commercial dans la section '
                '« Liste des commerciaux ».'
            )

        zone_existante = Zone.objects.filter(nom=zone_nom).first()
        if zone_existante is None and not delegation_id:
            return _echec('La délégation est obligatoire pour créer une nouvelle zone.')
        if zone_existante is None and delegation_obj is None:
            return _echec('La délégation sélectionnée est introuvable.')

        try:
            with transaction.atomic():
                zone_obj = zone_existante or Zone.objects.create(delegation=delegation_obj, nom=zone_nom)
                client.zone = zone_obj
                client = form.save()

                if is_admin:
                    client.client_users.all().delete()
                    ClientUser.objects.bulk_create([
                        ClientUser(client=client, user_id=uid) for uid in commercial_ids
                    ])
        except Exception:
            messages.error(
                request,
                "Échec de la modification du client : aucune donnée n'a été modifiée.",
            )
            return render(request, self.template_name, self._context(
                request, client,
                selected_gouvernorat_id=gouvernorat_id_preserve,
                selected_delegation_id=delegation_id,
                zone_nom_value=zone_nom,
                selected_commercial_ids_json=json.dumps(commercial_ids),
            ))

        log_audit(
            AuditAction.UPDATE, f'Modification client « {client} »',
            table='Client', record_id=client.pk, old_value=avant, new_value=model_to_dict(client),
        )
        messages.success(request, f'Client « {client} » modifié avec succès.')
        return redirect('commercial:client-list')


class ClientDeleteView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']

    def post(self, request, pk):
        client = get_object_or_404(_client_queryset_for_user(request.user), pk=pk)
        nom = str(client)

        if not _is_administration(request.user):
            messages.error(
                request,
                'Seuls les utilisateurs du groupe Administration peuvent supprimer un client.',
            )
            return redirect('commercial:client-list')

        if client.ventes_code.exists() or client.bons_livraison_code.exists() or client.agendas.exists():
            messages.error(
                request,
                f"Impossible de supprimer « {nom} » : cet enregistrement est encore lié à "
                "des ventes, des bons de livraison ou à l'agenda.",
            )
            return redirect('commercial:client-list')

        avant = model_to_dict(client)
        try:
            with transaction.atomic():
                client.client_users.all().delete()
                client.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer « {nom} » : cet enregistrement est encore lié à "
                "d'autres tables.",
            )
        else:
            log_audit(
                AuditAction.DELETE, f'Suppression client « {nom} »',
                table='Client', record_id=pk, old_value=avant,
            )
            messages.success(request, f'Client « {nom} » supprimé avec succès.')
        return redirect('commercial:client-list')


class ClientSansBLListView(GroupRequiredMixin, View):
    """Page « Liste des clients sans bon de livraison pour la période » :
    clients actifs, rattachés au commercial de la page, n'ayant aucun bon de
    livraison dont la date tombe dans [date_debut, date_fin]."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/client/sans_bl_list.html'
    PAGINATE_BY = 6
    NB_BL_JOURS = 180

    def get(self, request):
        today = date.today()
        is_admin = _is_administration(request.user)

        commercial_id  = request.GET.get('commercial_id', '').strip() if is_admin else str(request.user.pk)
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str   = request.GET.get('date_fin', '').strip()
        gouvernorat_id = request.GET.get('gouvernorat', '').strip()
        delegation_id  = request.GET.get('delegation', '').strip()
        zone_id        = request.GET.get('zone', '').strip()

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        if date_debut > date_fin:
            messages.error(request, 'La date de début doit être antérieure ou égale à la date de fin.')
            date_debut = today - timedelta(days=6)
            date_fin = today
            date_debut_str = date_debut.isoformat()
            date_fin_str = date_fin.isoformat()

        # ── Clients ayant au moins un bon de livraison sur la période ──────
        clients_avec_bl = BonLivraisonCode.objects.filter(
            bon_livraison_at__gte=date_debut, bon_livraison_at__lte=date_fin,
        ).values_list('client_id', flat=True)

        qs = Client.objects.filter(is_active=True).exclude(pk__in=clients_avec_bl)
        if is_admin:
            if commercial_id:
                qs = qs.filter(client_users__user_id=commercial_id)
        else:
            qs = qs.filter(client_users__user=request.user)

        if zone_id:
            qs = qs.filter(zone_id=zone_id)
        elif delegation_id:
            qs = qs.filter(zone__delegation_id=delegation_id)
        elif gouvernorat_id:
            qs = qs.filter(zone__delegation__gouvernorat_id=gouvernorat_id)

        six_mois_debut = today - timedelta(days=self.NB_BL_JOURS)
        qs = qs.annotate(
            nb_bl=Count(
                'bons_livraison_code',
                filter=Q(bons_livraison_code__bon_livraison_at__gte=six_mois_debut),
                distinct=True,
            ),
        ).select_related('zone').order_by('zone__nom', 'raison_sociale').distinct()

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        delegations_list = Delegation.objects.select_related('gouvernorat').order_by(
            'gouvernorat__nom', 'nom_delegation',
        )
        zones_list = Zone.objects.select_related('delegation__gouvernorat').order_by(
            'delegation__gouvernorat__nom', 'delegation__nom_delegation', 'nom',
        )

        return render(request, self.template_name, {
            'page_obj':          page_obj,
            'is_paginated':      page_obj.has_other_pages(),
            'clients':           page_obj.object_list,
            'total_count':       paginator.count,
            'is_administration': is_admin,
            'commercial_id':     commercial_id,
            'date_debut':        date_debut_str,
            'date_fin':          date_fin_str,
            'gouvernorat_id':    gouvernorat_id,
            'delegation_id':     delegation_id,
            'zone_id':           zone_id,
            'commerciaux_list':  _agenda_commerciaux_list() if is_admin else None,
            'gouvernorats_list': Gouvernorat.objects.order_by('nom'),
            'delegations_list':  delegations_list,
            'zones_list':        zones_list,
            'bl_agenda_date_debut': (today - timedelta(days=self.NB_BL_JOURS)).isoformat(),
            'bl_agenda_date_fin':   today.isoformat(),
        })


# ─── Bons de livraison ───────────────────────────────────────────────────────

_UNITS_LETTRES = [
    '', 'un', 'deux', 'trois', 'quatre', 'cinq', 'six', 'sept', 'huit', 'neuf',
    'dix', 'onze', 'douze', 'treize', 'quatorze', 'quinze', 'seize',
    'dix-sept', 'dix-huit', 'dix-neuf',
]
_TENS_WORDS_LETTRES = {
    2: 'vingt', 3: 'trente', 4: 'quarante', 5: 'cinquante', 6: 'soixante', 8: 'quatre-vingt',
}


def _deux_chiffres_en_lettres(n):
    if n < 20:
        return _UNITS_LETTRES[n]
    tens, rest = divmod(n, 10)
    if tens == 7:
        tens, rest = 6, rest + 10
    elif tens == 9:
        tens, rest = 8, rest + 10
    mot = _TENS_WORDS_LETTRES[tens]
    if rest == 0:
        return mot
    if rest == 1 and tens != 8:
        return mot + '-et-un'
    return mot + '-' + _UNITS_LETTRES[rest]


def _trois_chiffres_en_lettres(n):
    if n < 100:
        return _deux_chiffres_en_lettres(n)
    centaines, rest = divmod(n, 100)
    prefixe = 'cent' if centaines == 1 else _UNITS_LETTRES[centaines] + ' cent'
    if rest == 0:
        return prefixe
    return prefixe + ' ' + _deux_chiffres_en_lettres(rest)


def _nombre_en_lettres(n):
    if n == 0:
        return 'zéro'
    millions, rest = divmod(n, 1_000_000)
    milliers, unites = divmod(rest, 1000)
    parts = []
    if millions:
        parts.append('un million' if millions == 1 else _trois_chiffres_en_lettres(millions) + ' millions')
    if milliers:
        parts.append('mille' if milliers == 1 else _trois_chiffres_en_lettres(milliers) + ' mille')
    if unites or not parts:
        parts.append(_trois_chiffres_en_lettres(unites))
    return ' '.join(parts)


def _montant_en_lettres(value):
    """Convertit un montant (dinars, 3 décimales = millimes) en toutes lettres,
    ex: Decimal('20.600') -> 'Vingt dinars et six cent millimes'."""
    value = Decimal(value).quantize(Decimal('0.001'))
    dinars = int(value)
    millimes = int((value - dinars) * 1000)
    parts = []
    if dinars or not millimes:
        parts.append(_nombre_en_lettres(dinars) + (' dinar' if dinars == 1 else ' dinars'))
    if millimes:
        parts.append(_nombre_en_lettres(millimes) + (' millime' if millimes == 1 else ' millimes'))
    phrase = ' et '.join(parts)
    return phrase[0].upper() + phrase[1:]


def _format_montant_tnd(value):
    """Formate un montant en chiffres façon TND : séparateur de milliers
    espace, virgule décimale — ex: Decimal('1234.560') -> '1 234,560'."""
    texte = f'{value:,.3f}'
    return texte.replace(',', ' ').replace('.', ',')


def _erreur_suppression_bon_livraison(bl, is_admin_user, nb_bl_du_code, transaction_liee):
    """Retourne le message d'erreur bloquant la suppression de ce
    BonLivraison, ou '' si toutes les conditions sont respectées. Utilisée à
    la fois pour l'affichage (griser/expliquer le bouton Supprimer dans la
    liste) et pour la validation serveur réelle au moment de la suppression —
    ne JAMAIS faire confiance au seul calcul côté liste."""
    code = bl.bon_livraison_code
    if code.hist_stock_initial_id is not None:
        return 'Les quantités des produits livrais sont déjà soldés'
    if code.vente_code_id is not None:
        return 'La bon de livraison est déjà lié à une vente'
    if nb_bl_du_code > 1:
        return 'Le bon de livraison est déjà modifié'
    if transaction_liee is not None and transaction_liee.hist_solde_compte_id is not None:
        return 'le Bon de livraison est déjà lié à un solde de compte du commercial'
    if not is_admin_user:
        return "Vous n'êtes pas autorisé à supprimer un bon de livraison"
    return ''


class BonLivraisonListView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_livraison/list.html'
    PAGINATE_BY = 8

    def get(self, request):
        is_admin = _is_administration(request.user)
        today = date.today()

        # ── Lecture des filtres GET ───────────────────────────────
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str   = request.GET.get('date_fin', '').strip()
        client_id      = request.GET.get('client_id', '').strip()
        zone_id        = request.GET.get('zone_id', '').strip()
        statut         = request.GET.get('statut', '').strip()
        commercial_id  = request.GET.get('commercial_id', '').strip() if is_admin else ''

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        if date_debut > date_fin:
            messages.error(request, 'La date de début doit être antérieure ou égale à la date de fin.')
            date_debut = today - timedelta(days=6)
            date_fin = today
            date_debut_str = date_debut.isoformat()
            date_fin_str = date_fin.isoformat()

        # ── Queryset filtré ────────────────────────────────────────
        # La table BonLivraisonCode est un historique (voir BonLivraison) : un
        # même BonLivraisonCode peut avoir plusieurs BonLivraison. On affiche
        # chaque BonLivraisonCode de la période via uniquement son DERNIER
        # BonLivraison (celui dont l'id — bon_livraison_id — est le plus grand).
        dernier_bl_id_par_code = (
            BonLivraison.objects
            .filter(
                bon_livraison_code__bon_livraison_at__gte=date_debut,
                bon_livraison_code__bon_livraison_at__lte=date_fin,
            )
            .values('bon_livraison_code_id')
            .annotate(dernier_id=Max('id'))
            .values_list('dernier_id', flat=True)
        )
        qs = BonLivraison.objects.select_related(
            'user', 'bon_livraison_code__client__zone__delegation',
        ).filter(pk__in=dernier_bl_id_par_code)

        if not is_admin:
            qs = qs.filter(user=request.user)
        elif commercial_id:
            qs = qs.filter(user_id=commercial_id)

        if client_id:
            qs = qs.filter(bon_livraison_code__client_id=client_id)
        if zone_id:
            qs = qs.filter(bon_livraison_code__client__zone_id=zone_id)
        if statut:
            qs = qs.filter(statut=statut)

        qs = qs.order_by('-bon_livraison_code__bon_livraison_at', 'bon_livraison_code__bon_livraison_numero')

        # ── Données pour les listes déroulantes ───────────────────
        if is_admin:
            clients_list = Client.objects.order_by('raison_sociale')
            zones_list = Zone.objects.order_by('nom')
        else:
            clients_scope = _client_queryset_for_user(request.user)
            clients_list = clients_scope.order_by('raison_sociale')
            zones_list = Zone.objects.filter(clients__in=clients_scope).distinct().order_by('nom')

        commerciaux_list = (
            User.objects.filter(groups__name='Commercial').order_by('last_name', 'first_name')
            if is_admin else None
        )

        # ── Pagination ────────────────────────────────────────────
        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj  = paginator.get_page(request.GET.get('page', 1))

        # ── Éligibilité à la suppression (calculée pour la page affichée) ──
        rows = list(page_obj.object_list)
        nb_bl_par_code = dict(
            BonLivraison.objects
            .filter(bon_livraison_code_id__in=[bl.bon_livraison_code_id for bl in rows])
            .values('bon_livraison_code_id')
            .annotate(nb=Count('id'))
            .values_list('bon_livraison_code_id', 'nb')
        )
        transactions_par_table_id = {
            t.table_id: t
            for t in Transaction.objects.filter(
                table_id__in=[f'BonLivraison_id_{bl.pk}' for bl in rows],
            )
        }
        for bl in rows:
            bl.erreur_suppression = _erreur_suppression_bon_livraison(
                bl, is_admin,
                nb_bl_par_code.get(bl.bon_livraison_code_id, 1),
                transactions_par_table_id.get(f'BonLivraison_id_{bl.pk}'),
            )

        return render(request, self.template_name, {
            'page_obj':          page_obj,
            'is_paginated':      page_obj.has_other_pages(),
            'bons_livraison':    rows,
            'is_administration': is_admin,
            'date_debut':        date_debut_str,
            'date_fin':          date_fin_str,
            'client_id':         client_id,
            'zone_id':           zone_id,
            'statut':            statut,
            'commercial_id':     commercial_id,
            'clients_list':      clients_list,
            'zones_list':        zones_list,
            'commerciaux_list':  commerciaux_list,
            'statut_choices':    STATUT_PAIEMENT_CHOICES,
        })


class BonLivraisonDetailView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_livraison/detail.html'

    def get(self, request, pk):
        qs = BonLivraison.objects.select_related(
            'user', 'bon_livraison_code__client__zone__delegation__gouvernorat',
        )
        if not _is_administration(request.user):
            qs = qs.filter(user=request.user)
        bon_livraison = get_object_or_404(qs, pk=pk)
        details = bon_livraison.details.select_related('produit').order_by('pk')

        return render(request, self.template_name, {
            'bon_livraison':      bon_livraison,
            'bon_livraison_code': bon_livraison.bon_livraison_code,
            'details':            details,
        })


class BonLivraisonDeleteView(GroupRequiredMixin, View):
    """Suppression d'un bon de livraison (voir _erreur_suppression_bon_livraison
    pour les conditions bloquantes). Ordre de suppression : Transaction liée,
    puis BonLivraisonDetaille, puis BonLivraison, puis BonLivraisonCode —
    entièrement dans une transaction atomique."""
    group_required = ['Administration', 'Commercial']

    def post(self, request, pk):
        bl = get_object_or_404(
            BonLivraison.objects.select_related('bon_livraison_code'), pk=pk,
        )
        code = bl.bon_livraison_code
        numero = str(code)

        nb_bl_du_code = BonLivraison.objects.filter(bon_livraison_code_id=code.pk).count()
        table_id = f'BonLivraison_id_{bl.pk}'
        transaction_liee = Transaction.objects.filter(table_id=table_id).first()

        erreur = _erreur_suppression_bon_livraison(
            bl, _is_administration(request.user), nb_bl_du_code, transaction_liee,
        )
        if erreur:
            messages.error(request, erreur)
            return redirect('commercial:bon-livraison-list')

        avant = {
            'bon_livraison_code': model_to_dict(code),
            'bon_livraison': model_to_dict(bl),
            'details': [model_to_dict(d) for d in bl.details.all()],
        }

        try:
            with transaction.atomic():
                if transaction_liee is not None:
                    transaction_liee.delete()
                bl.details.all().delete()
                bl.delete()
                code.delete()
        except Exception:
            messages.error(
                request,
                f"Échec de la suppression du bon de livraison {numero} : aucune donnée n'a été modifiée.",
            )
            return redirect('commercial:bon-livraison-list')

        log_audit(
            AuditAction.DELETE, f'Suppression bon de livraison {numero}',
            table='BonLivraisonCode', record_id=code.pk, old_value=avant,
        )
        messages.success(request, f'Bon de livraison {numero} supprimé avec succès.')
        return redirect('commercial:bon-livraison-list')


class BonLivraisonUpdateView(GroupRequiredMixin, View):
    """Formulaire « Modifier bon de livraison » : Commercial → Zone → Client en
    cascade, Section 2 limitée aux lignes existantes (seul le prix unitaire est
    modifiable), et enregistrement (voir post()) qui crée un nouvel
    enregistrement historique dans commercial_bonlivraison plutôt que de
    modifier l'existant."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_livraison/update.html'

    def _get_bon_livraison(self, request, pk):
        is_admin = _is_administration(request.user)
        qs = BonLivraison.objects.select_related('user', 'bon_livraison_code__client__zone')
        if not is_admin:
            qs = qs.filter(user=request.user)
        return get_object_or_404(qs, pk=pk), is_admin

    def _context(self, request, bon_livraison, is_admin, **overrides):
        code = bon_livraison.bon_livraison_code
        details = bon_livraison.details.select_related('produit').order_by('pk')

        if is_admin:
            clients_qs = Client.objects.select_related('zone').order_by('raison_sociale')
            zones_qs = Zone.objects.order_by('nom')
            client_users_qs = ClientUser.objects.all()
            commerciaux_list = User.objects.filter(groups__name='Commercial').order_by('last_name', 'first_name')
        else:
            clients_scope = _client_queryset_for_user(request.user)
            clients_qs = clients_scope.select_related('zone').order_by('raison_sociale')
            zones_qs = Zone.objects.filter(clients__in=clients_scope).distinct().order_by('nom')
            client_users_qs = ClientUser.objects.filter(user=request.user)
            commerciaux_list = None

        prix_overrides = overrides.pop('prix_overrides', {})
        detail_rows = [
            {
                'pk': d.pk,
                'produit': d.produit,
                'quantite': d.quantite,
                'prix_unitaire': prix_overrides.get(str(d.pk), d.prix_unitaire),
            }
            for d in details
        ]

        context = {
            'bon_livraison':      bon_livraison,
            'bon_livraison_code': code,
            'detail_rows':        detail_rows,
            'is_administration':  is_admin,
            'commerciaux_list':   commerciaux_list,
            'mode_paiement_choices': MODE_PAIEMENT_CHOICES,
            'clients_json': json.dumps([
                {'id': c.pk, 'label': str(c), 'zone_id': c.zone_id} for c in clients_qs
            ]),
            'zones_json': json.dumps([
                {'id': z.pk, 'nom': z.nom} for z in zones_qs
            ]),
            'client_users_json': json.dumps([
                {'client_id': cu.client_id, 'user_id': cu.user_id} for cu in client_users_qs
            ]),
            'selected_client_id':     str(code.client_id),
            'selected_zone_id':       str(code.client.zone_id) if code.client.zone_id else '',
            'selected_commercial_id': str(bon_livraison.user_id),
            'nouveau_acompte_value':  '0.000',
            'mode_paiement_value':    'Espèces',
            'observation_value':      bon_livraison.observation or '',
        }
        context.update(overrides)
        return context

    def get(self, request, pk):
        bon_livraison, is_admin = self._get_bon_livraison(request, pk)
        return render(request, self.template_name, self._context(request, bon_livraison, is_admin))

    def post(self, request, pk):
        bon_livraison, is_admin = self._get_bon_livraison(request, pk)
        code = bon_livraison.bon_livraison_code

        client_id     = request.POST.get('client_id', '').strip()
        commercial_id = request.POST.get('commercial_id', '').strip() if is_admin else str(request.user.pk)
        nouveau_acompte_s = request.POST.get('nouveau_acompte', '').strip()
        mode_paiement = request.POST.get('mode_paiement', '').strip() or 'Espèces'
        observation   = request.POST.get('observation', '').strip()

        detail_ids        = request.POST.getlist('detail_id')
        prix_unitaire_strs = request.POST.getlist('detail_prix_unitaire')
        prix_overrides = dict(zip(detail_ids, prix_unitaire_strs))

        def _echec(message):
            messages.error(request, message)
            submitted_client = Client.objects.filter(pk=client_id).first() if client_id else None
            context = self._context(
                request, bon_livraison, is_admin,
                selected_client_id=client_id or str(code.client_id),
                selected_zone_id=(
                    str(submitted_client.zone_id) if submitted_client and submitted_client.zone_id
                    else (str(code.client.zone_id) if code.client.zone_id else '')
                ),
                selected_commercial_id=commercial_id or str(bon_livraison.user_id),
                nouveau_acompte_value=nouveau_acompte_s,
                mode_paiement_value=mode_paiement,
                observation_value=observation,
                prix_overrides=prix_overrides,
            )
            return render(request, self.template_name, context)

        # ── Étape 1 : validations ─────────────────────────────────────────────
        if is_admin and not commercial_id:
            return _echec('il faut définir le commercial')
        if not client_id:
            return _echec('il faut définir le client')

        nouveau_acompte = _decimal_or_zero(nouveau_acompte_s)
        if nouveau_acompte < 0:
            return _echec('il faut que le nouveau montant payé est suppérieur ou égal à zéro')

        client = Client.objects.filter(pk=client_id).first()
        if client is None:
            return _echec('Le client sélectionné est introuvable.')

        commercial_user = User.objects.filter(pk=commercial_id).first()
        if commercial_user is None:
            return _echec('Le commercial sélectionné est introuvable.')

        details_par_id = {str(d.pk): d for d in bon_livraison.details.all()}
        lignes = []
        for detail_id, prix_s in zip(detail_ids, prix_unitaire_strs):
            d = details_par_id.get(detail_id)
            if d is None:
                continue
            prix = _quantize(_decimal_or_zero(prix_s))
            if prix < 0:
                return _echec('il faut que tous les prix unitaires soient suppérieur ou égal à zéro')
            lignes.append({'produit_id': d.produit_id, 'quantite': d.quantite, 'prix_unitaire': prix})

        total_montant = sum(
            (_quantize(l['quantite'] * l['prix_unitaire']) for l in lignes), Decimal('0'),
        )
        ancien_total_acompte = bon_livraison.total_acompte
        total_acompte = ancien_total_acompte + nouveau_acompte
        reste_a_payer = total_montant - total_acompte
        if reste_a_payer < 0:
            return _echec('il faut que le montant reste à payer est suppérieur ou égal à zéro')

        if reste_a_payer == 0:
            statut = 'Payé'
        elif 0 < reste_a_payer < total_montant:
            statut = 'Partiellement payé'
        elif reste_a_payer == total_montant:
            statut = 'Non payé'
        else:
            statut = ''

        # ── Étape 2 : écriture atomique ────────────────────────────────────────
        try:
            with transaction.atomic():
                if code.client_id != client.pk:
                    code.client = client
                    code.save(update_fields=['client'])

                nouveau_bl = BonLivraison.objects.create(
                    bon_livraison_code=code,
                    user=commercial_user,
                    observation=observation or None,
                    mode_paiement=mode_paiement,
                    nouveau_acompte=nouveau_acompte,
                    total_acompte=total_acompte,
                )
                BonLivraisonDetaille.objects.bulk_create([
                    BonLivraisonDetaille(
                        bon_livraison=nouveau_bl,
                        produit_id=l['produit_id'],
                        quantite=l['quantite'],
                        prix_unitaire=l['prix_unitaire'],
                        total_ligne=_quantize(l['quantite'] * l['prix_unitaire']),
                    )
                    for l in lignes
                ])

                nouveau_bl.total_montant = total_montant
                nouveau_bl.reste_a_payer = reste_a_payer
                nouveau_bl.statut = statut
                nouveau_bl.save(update_fields=['total_montant', 'reste_a_payer', 'statut'])

                libelle = LibelleTransaction.objects.get_or_create(
                    libelle='Paiement bon de livraison modifié',
                )[0]
                Transaction.objects.create(
                    user=nouveau_bl.user,
                    created_at=date.today(),
                    libelle=libelle,
                    table_id=f'BonLivraison_id_{nouveau_bl.pk}',
                    montant=nouveau_acompte,
                )
        except Exception:
            messages.error(
                request,
                "Échec de la modification du bon de livraison : aucune donnée n'a été modifiée.",
            )
            context = self._context(
                request, bon_livraison, is_admin,
                selected_client_id=client_id,
                selected_zone_id=str(client.zone_id) if client.zone_id else '',
                selected_commercial_id=commercial_id,
                nouveau_acompte_value=nouveau_acompte_s,
                mode_paiement_value=mode_paiement,
                observation_value=observation,
                prix_overrides=prix_overrides,
            )
            return render(request, self.template_name, context)

        log_audit(
            AuditAction.UPDATE, f'Modification bon de livraison {code}',
            table='BonLivraisonCode', record_id=code.pk,
            old_value=model_to_dict(bon_livraison), new_value=model_to_dict(nouveau_bl),
        )
        messages.success(request, f'Bon de livraison {code} modifié avec succès.')
        return redirect('commercial:bon-livraison-list')


class BonLivraisonCreateView(GroupRequiredMixin, View):
    """Formulaire « Nouveau bon de livraison » : filtres en cascade
    Commercial → Zone → Client, section « Détail par défaut » préremplie via
    AJAX, calculs de paiement côté client, et enregistrement (voir post()).

    post() — dans une transaction.atomic() : création de BonLivraisonCode
    (numéro recalculé côté serveur) et BonLivraison (total_acompte =
    nouveau_acompte), regroupement des lignes valides par (produit,
    prix_unitaire) puis bulk_create des BonLivraisonDetaille, mise à jour de
    BonLivraison (total_montant, reste_a_payer, statut), décrément du stock
    de chaque Produit livré (production_produit.stock -= quantité livrée,
    jamais négatif — remis à 0 si le résultat le serait), puis création de la
    Transaction caisse (montant = nouveau_acompte). En cas d'échec de
    validation, la page est régénérée avec toutes les valeurs saisies et les
    lignes de détail préservées (préremplissage JSON, comme pour
    achats/nouveau)."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_livraison/create.html'

    def _context(self, request, **overrides):
        is_admin = _is_administration(request.user)
        today = date.today()

        if is_admin:
            commerciaux_list = User.objects.filter(groups__name='Commercial').order_by('last_name', 'first_name')
            clients_qs = Client.objects.order_by('raison_sociale')
            zones_qs = Zone.objects.order_by('nom')
            client_users_qs = ClientUser.objects.all()
        else:
            commerciaux_list = None
            clients_scope = _client_queryset_for_user(request.user)
            clients_qs = clients_scope.order_by('raison_sociale')
            zones_qs = Zone.objects.filter(clients__in=clients_scope).distinct().order_by('nom')
            client_users_qs = ClientUser.objects.filter(user=request.user)

        produits_list = Produit.objects.order_by('nom')

        context = {
            'is_administration':     is_admin,
            'today':                 today.isoformat(),
            'numero_initial':        BonLivraisonCode.objects.filter(bon_livraison_at=today).count() + 1,
            'mode_paiement_choices': MODE_PAIEMENT_CHOICES,
            'commerciaux_list':      commerciaux_list,
            'clients_json': json.dumps([
                {'id': c.pk, 'label': str(c), 'zone_id': c.zone_id} for c in clients_qs
            ]),
            'zones_json': json.dumps([
                {'id': z.pk, 'nom': z.nom} for z in zones_qs
            ]),
            'client_users_json': json.dumps([
                {'client_id': cu.client_id, 'user_id': cu.user_id} for cu in client_users_qs
            ]),
            'produits_json': json.dumps([
                {'id': p.pk, 'nom': p.nom, 'prix_unitaire': str(p.prix_unitaire)} for p in produits_list
            ]),
            'selected_commercial_id': '',
            'selected_client_id':     '',
            'bon_livraison_at_value': today.isoformat(),
            'mode_paiement_value':    'Espèces',
            'nouveau_acompte_value':  '',
            'observation_value':      '',
            'preserved_lignes_json':  '[]',
            'is_locked':              False,
            'saved_bon_livraison':    None,
            'saved_details':          None,
        }
        context.update(overrides)
        return context

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        is_admin = _is_administration(request.user)

        bl_at_str        = request.POST.get('bon_livraison_at', '').strip()
        client_id         = request.POST.get('client_id', '').strip()
        # Le champ Commercial n'est éditable qu'en Administration : pour un
        # utilisateur Commercial, on ignore toute valeur soumise et on impose
        # son propre ID (le champ est caché/figé côté template).
        commercial_id     = request.POST.get('commercial_id', '').strip() if is_admin else str(request.user.pk)
        nouveau_acompte_s = request.POST.get('nouveau_acompte', '').strip()
        mode_paiement     = request.POST.get('mode_paiement', '').strip() or 'Espèces'
        observation       = request.POST.get('observation', '').strip()

        produit_ids     = request.POST.getlist('detail_produit_id')
        quantites       = request.POST.getlist('detail_quantite')
        prix_unitaires  = request.POST.getlist('detail_prix_unitaire')
        raw_lignes = [
            {'produit_id': p, 'quantite': q, 'prix_unitaire': pu}
            for p, q, pu in zip(produit_ids, quantites, prix_unitaires)
        ]

        def _echec(message):
            messages.error(request, message)
            bl_at_pour_numero = date.fromisoformat(bl_at_str) if _is_iso_date(bl_at_str) else date.today()
            context = self._context(
                request,
                bon_livraison_at_value=bl_at_str or date.today().isoformat(),
                numero_initial=BonLivraisonCode.objects.filter(bon_livraison_at=bl_at_pour_numero).count() + 1,
                selected_commercial_id=commercial_id,
                selected_client_id=client_id,
                mode_paiement_value=mode_paiement,
                nouveau_acompte_value=nouveau_acompte_s,
                observation_value=observation,
                preserved_lignes_json=json.dumps(raw_lignes),
            )
            return render(request, self.template_name, context)

        # ── Étape 1 : validations des champs obligatoires ─────────────────────
        if not bl_at_str or not _is_iso_date(bl_at_str):
            return _echec('La date du bon de livraison est obligatoire.')
        bl_at = date.fromisoformat(bl_at_str)

        if not client_id:
            return _echec('Le client est obligatoire.')

        if not commercial_id:
            return _echec('Le commercial est obligatoire.')

        # ── Étape 2 : lignes valides (sections 3 + 4 réunies) ─────────────────
        # Ignorées silencieusement : produit absent, quantité ou prix ≤ 0.
        lignes_valides = []
        for p, q, pu in zip(produit_ids, quantites, prix_unitaires):
            p = (p or '').strip()
            if not p:
                continue
            qte = _decimal_or_zero(q).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            prix = _quantize(_decimal_or_zero(pu))
            if qte <= 0 or prix <= 0:
                continue
            lignes_valides.append({'produit_id': p, 'quantite': qte, 'prix_unitaire': prix})

        if not lignes_valides:
            return _echec(
                'Aucune ligne de détail valide : ajoutez au moins un produit avec une '
                'quantité et un prix unitaire strictement positifs.'
            )

        # ── Étape 3 : regroupement par (produit, prix unitaire) ───────────────
        groupes = {}
        for l in lignes_valides:
            key = (l['produit_id'], l['prix_unitaire'])
            groupes[key] = groupes.get(key, Decimal('0')) + l['quantite']

        produits_ids = {pid for pid, _prix in groupes}
        produits_par_id = {str(p.pk): p for p in Produit.objects.filter(pk__in=produits_ids)}
        if len(produits_par_id) != len(produits_ids):
            return _echec(
                "Un ou plusieurs produits sélectionnés n'existent plus. "
                'Veuillez rafraîchir la page et recommencer.'
            )

        client = Client.objects.filter(pk=client_id).first()
        if client is None:
            return _echec('Le client sélectionné est introuvable.')

        commercial_user = User.objects.filter(pk=commercial_id).first()
        if commercial_user is None:
            return _echec('Le commercial sélectionné est introuvable.')

        # ── Étape 4 : Montant payé / Montant total / Reste à payer ────────────
        nouveau_acompte = _decimal_or_zero(nouveau_acompte_s)
        if nouveau_acompte < 0:
            nouveau_acompte = Decimal('0')

        total_montant = sum(
            (_quantize(qte * prix) for (_pid, prix), qte in groupes.items()), Decimal('0'),
        )
        reste_a_payer = total_montant - nouveau_acompte
        if reste_a_payer < 0:
            return _echec('Le montant payé ne peut pas dépasser le montant total du bon de livraison.')

        if reste_a_payer == 0:
            statut = 'Payé'
        elif 0 < reste_a_payer < total_montant:
            statut = 'Partiellement payé'
        elif reste_a_payer == total_montant:
            statut = 'Non payé'
        else:
            statut = ''

        # ── Étape 5 : écriture atomique ────────────────────────────────────────
        try:
            with transaction.atomic():
                code = BonLivraisonCode.objects.create(
                    bon_livraison_at=bl_at,
                    bon_livraison_numero=BonLivraisonCode.objects.filter(bon_livraison_at=bl_at).count() + 1,
                    client=client,
                )
                bon_livraison = BonLivraison.objects.create(
                    bon_livraison_code=code,
                    user=commercial_user,
                    nouveau_acompte=nouveau_acompte,
                    total_acompte=nouveau_acompte,
                    mode_paiement=mode_paiement,
                    observation=observation or None,
                )
                BonLivraisonDetaille.objects.bulk_create([
                    BonLivraisonDetaille(
                        bon_livraison=bon_livraison,
                        produit=produits_par_id[pid],
                        quantite=qte,
                        prix_unitaire=prix,
                        total_ligne=_quantize(qte * prix),
                    )
                    for (pid, prix), qte in groupes.items()
                ])

                bon_livraison.total_montant = total_montant
                bon_livraison.reste_a_payer = reste_a_payer
                bon_livraison.statut = statut
                bon_livraison.save(update_fields=['total_montant', 'reste_a_payer', 'statut'])

                # ── Mise à jour du stock des produits livrés (jamais négatif) ──
                for (pid, _prix), qte in groupes.items():
                    produit = produits_par_id[pid]
                    produit.stock = max(produit.stock - int(qte), 0)
                    produit.save(update_fields=['stock'])

                libelle = LibelleTransaction.objects.get_or_create(
                    libelle='Paiement nouveau bon de livraison',
                )[0]
                Transaction.objects.create(
                    user=bon_livraison.user,
                    created_at=date.today(),
                    libelle=libelle,
                    table_id=f'BonLivraison_id_{bon_livraison.pk}',
                    montant=nouveau_acompte,
                )
        except Exception:
            messages.error(
                request,
                "Échec de l'enregistrement du bon de livraison : aucune donnée n'a été modifiée.",
            )
            context = self._context(
                request,
                bon_livraison_at_value=bl_at_str,
                numero_initial=BonLivraisonCode.objects.filter(bon_livraison_at=bl_at).count() + 1,
                selected_commercial_id=commercial_id,
                selected_client_id=client_id,
                mode_paiement_value=mode_paiement,
                nouveau_acompte_value=nouveau_acompte_s,
                observation_value=observation,
                preserved_lignes_json=json.dumps(raw_lignes),
            )
            return render(request, self.template_name, context)

        log_audit(
            AuditAction.CREATE, f'Création bon de livraison {code} — {total_montant} TND',
            table='BonLivraisonCode', record_id=code.pk,
            new_value={
                'bon_livraison_code': model_to_dict(code),
                'bon_livraison': model_to_dict(bon_livraison),
            },
        )
        messages.success(request, f'Bon de livraison {code} enregistré avec succès.')

        # Après un enregistrement réussi, la page reste affichée mais
        # entièrement verrouillée (voir template) : le bouton Etat PDF
        # s'active, Annuler devient Fermer, Enregistrer reste désactivé.
        saved_details = bon_livraison.details.select_related('produit').order_by('pk')
        context = self._context(
            request,
            is_locked=True,
            saved_bon_livraison=bon_livraison,
            saved_details=saved_details,
        )
        return render(request, self.template_name, context)


class BonLivraisonPdfView(GroupRequiredMixin, View):
    """Génère l'« État PDF » d'un bon de livraison (pk = bon_livraison_id),
    imprimable/téléchargeable nativement via le lecteur PDF du navigateur."""
    group_required = ['Administration', 'Commercial']

    def get(self, request, pk):
        qs = BonLivraison.objects.select_related('user', 'bon_livraison_code__client__zone')
        if not _is_administration(request.user):
            qs = qs.filter(user=request.user)
        bon_livraison = get_object_or_404(qs, pk=pk)
        code = bon_livraison.bon_livraison_code
        details = bon_livraison.details.select_related('produit').order_by('pk')

        html = render_to_string('commercial/bon_livraison/pdf_etat.html', {
            'bon_livraison':      bon_livraison,
            'bon_livraison_code': code,
            'client':             code.client,
            'details':            details,
            'montant_lettres':    _montant_en_lettres(bon_livraison.total_montant),
            'montant_chiffres':   _format_montant_tnd(bon_livraison.total_montant),
        })

        pdf_buffer = BytesIO()
        pisa_status = pisa.CreatePDF(html, dest=pdf_buffer)
        if pisa_status.err:
            messages.error(request, 'Échec de la génération du PDF.')
            return redirect('commercial:bon-livraison-list')

        log_audit(
            AuditAction.EXPORT_PDF, f'Export PDF bon de livraison {code}',
            table='BonLivraison', record_id=bon_livraison.pk,
        )

        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        filename = f'bon_livraison_{code.bon_livraison_numero}_{code.bon_livraison_at.isoformat()}.pdf'
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response


class BonLivraisonNumeroSuivantView(GroupRequiredMixin, View):
    """Point d'entrée AJAX utilisé par le template Nouveau bon de livraison : à
    chaque changement du champ Date bon de livraison, recalcule le Numéro bon
    de livraison suivant pour cette date (nombre de BonLivraisonCode existants
    ce jour-là + 1)."""
    group_required = ['Administration', 'Commercial']

    def get(self, request):
        bl_at_str = request.GET.get('bon_livraison_at', '').strip()
        try:
            bl_at = date.fromisoformat(bl_at_str)
        except ValueError:
            bl_at = date.today()
        numero = BonLivraisonCode.objects.filter(bon_livraison_at=bl_at).count() + 1
        return JsonResponse({'bon_livraison_numero': numero})


def _derniers_prix_unitaire_par_produit(client):
    """Pour chaque produit déjà livré à ce client, retourne le prix_unitaire
    de son enregistrement le plus récent dans commercial_bonlivraisondetaille
    — en ne considérant, pour chaque BonLivraisonCode du client, que son
    DERNIER BonLivraison (historique le plus récent), cohérent avec le reste
    de l'application (liste des bons de livraison, section 3 par défaut)."""
    dernier_bl_id_par_code = (
        BonLivraison.objects
        .filter(bon_livraison_code__client=client)
        .values('bon_livraison_code_id')
        .annotate(dernier_id=Max('id'))
        .values_list('dernier_id', flat=True)
    )
    details = (
        BonLivraisonDetaille.objects
        .filter(bon_livraison_id__in=dernier_bl_id_par_code, produit__isnull=False)
        .order_by(
            'produit_id',
            '-bon_livraison__bon_livraison_code__bon_livraison_at',
            '-bon_livraison__bon_livraison_code__bon_livraison_numero',
            '-pk',
        )
    )
    derniers_prix = {}
    for d in details:
        derniers_prix.setdefault(d.produit_id, d.prix_unitaire)
    return derniers_prix


class BonLivraisonDefaultsView(GroupRequiredMixin, View):
    """Point d'entrée AJAX utilisé par le template Nouveau bon de livraison
    pour préremplir la section « Détail bon de livraison par défaut » : étant
    donné un client et une date, retourne les lignes du dernier BonLivraison
    (historique le plus récent) du dernier BonLivraisonCode de ce client dont
    la date tombe le même jour de la semaine que la date fournie — à défaut,
    celles du dernier BonLivraisonCode du client, tout court. Le prix_unitaire
    de chaque ligne est ensuite redéfini selon le dernier prix pratiqué pour ce
    même produit et ce même client (voir _derniers_prix_unitaire_par_produit),
    et cette même table de derniers prix est renvoyée (« derniers_prix ») pour
    préremplir le prix unitaire de la section 4 dès qu'un produit y est choisi."""
    group_required = ['Administration', 'Commercial']

    def get(self, request):
        client_id = request.GET.get('client_id', '').strip()
        bl_at_str = request.GET.get('bon_livraison_at', '').strip()

        if not client_id:
            return JsonResponse({'lignes': [], 'derniers_prix': {}})

        client = get_object_or_404(_client_queryset_for_user(request.user), pk=client_id)
        derniers_prix = _derniers_prix_unitaire_par_produit(client)
        derniers_prix_json = {str(pid): str(prix) for pid, prix in derniers_prix.items()}

        try:
            ref_date = date.fromisoformat(bl_at_str)
        except ValueError:
            ref_date = date.today()

        codes = list(
            BonLivraisonCode.objects.filter(client=client).only(
                'id', 'bon_livraison_at', 'bon_livraison_numero',
            )
        )
        if not codes:
            return JsonResponse({'lignes': [], 'derniers_prix': derniers_prix_json})

        meme_jour_semaine = [c for c in codes if c.bon_livraison_at.weekday() == ref_date.weekday()]
        candidats = meme_jour_semaine or codes
        code = max(candidats, key=lambda c: (c.bon_livraison_at, c.bon_livraison_numero))

        dernier_bl = code.bons_livraison.order_by('-pk').first()
        if dernier_bl is None:
            return JsonResponse({'lignes': [], 'derniers_prix': derniers_prix_json})

        lignes = [
            {
                'produit_id': d.produit_id,
                'quantite': str(d.quantite),
                'prix_unitaire': str(derniers_prix.get(d.produit_id, d.prix_unitaire)),
            }
            for d in dernier_bl.details.all().order_by('pk')
            if d.produit_id is not None
        ]
        return JsonResponse({'lignes': lignes, 'derniers_prix': derniers_prix_json})


# ─── Bons de sortie ───────────────────────────────────────────────────────────

def _erreur_suppression_bon_sortie(bs):
    """Retourne le message d'erreur bloquant la suppression (ou la
    modification) de ce BonSortie, ou '' si autorisé. Utilisée à la fois
    pour l'affichage (griser/expliquer le bouton dans la liste) et pour la
    validation serveur réelle au moment de l'action — ne jamais faire
    confiance au seul calcul côté liste."""
    if bs.historique_stock_initial_id is not None:
        return 'Un bon de sortie soldé ne peut être supprimé.'
    return ''


class BonSortieListView(GroupRequiredMixin, View):
    """Liste des BonSortie (production.models) sur une période — filtrée par
    commercial : verrouillée sur l'utilisateur connecté (champ masqué) s'il
    appartient au groupe Commercial, sinon un filtre déroulant (par défaut
    « tous les commerciaux ») est proposé. Triée par Commercial croissant puis
    sortie_at décroissant, paginée à 10 lignes. « Soldé » = historique_stock_
    initial non null. « Visualiser » ouvre une pop-up (voir
    BonSortieDetailPopupView) ; « Modifier »/« Supprimer » sont actifs
    uniquement si non soldé (voir BonSortieUpdateView / BonSortieDeleteView)."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_sortie/list.html'
    PAGINATE_BY = 10

    def get(self, request):
        is_commercial = _is_commercial(request.user)
        today = date.today()

        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str = request.GET.get('date_fin', '').strip()
        commercial_id = request.GET.get('commercial_id', '').strip() if not is_commercial else ''

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        if date_debut > date_fin:
            messages.error(request, 'La date de début doit être antérieure ou égale à la date de fin.')
            date_debut = today - timedelta(days=6)
            date_fin = today
            date_debut_str = date_debut.isoformat()
            date_fin_str = date_fin.isoformat()

        qs = BonSortie.objects.select_related('user').filter(
            sortie_at__gte=date_debut, sortie_at__lte=date_fin,
        )

        if is_commercial:
            qs = qs.filter(user=request.user)
        elif commercial_id:
            qs = qs.filter(user_id=commercial_id)

        qs = qs.order_by('user__last_name', 'user__first_name', '-sortie_at')

        commerciaux_list = (
            None if is_commercial
            else User.objects.filter(groups__name='Commercial').order_by('last_name', 'first_name')
        )

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        rows = list(page_obj.object_list)
        for bs in rows:
            bs.erreur_suppression = _erreur_suppression_bon_sortie(bs)

        return render(request, self.template_name, {
            'page_obj':         page_obj,
            'is_paginated':     page_obj.has_other_pages(),
            'bons_sortie':      rows,
            'is_commercial':    is_commercial,
            'date_debut':       date_debut_str,
            'date_fin':         date_fin_str,
            'commercial_id':    commercial_id,
            'commerciaux_list': commerciaux_list,
        })


def _erreur_suppression_bon_restitution(br):
    """Retourne le message d'erreur bloquant la suppression (ou la
    modification) de ce BonRestitution, ou '' si autorisé. Utilisée à la
    fois pour l'affichage (griser/expliquer le bouton dans la liste) et pour
    la validation serveur réelle au moment de l'action — ne jamais faire
    confiance au seul calcul côté liste."""
    if br.historique_stock_initial_id is not None:
        return 'Un bon de restitution soldé ne peut être supprimé.'
    return ''


class BonRestitutionListView(GroupRequiredMixin, View):
    """Liste des BonRestitution (production.models) sur une période —
    filtrée par commercial : verrouillée sur l'utilisateur connecté (champ
    masqué) s'il appartient au groupe Commercial, sinon un filtre déroulant
    (par défaut « tous les commerciaux ») est proposé. Triée par Commercial
    croissant puis restitution_at décroissant, paginée à 10 lignes.
    « Soldé » = historique_stock_initial non null. « Visualiser » ouvre une
    pop-up (voir BonRestitutionDetailPopupView) ; « Modifier »/« Supprimer »
    sont actifs uniquement si non soldé (voir BonRestitutionUpdateView /
    BonRestitutionDeleteView)."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_restitution/list.html'
    PAGINATE_BY = 10

    def get(self, request):
        is_commercial = _is_commercial(request.user)
        today = date.today()

        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str = request.GET.get('date_fin', '').strip()
        commercial_id = request.GET.get('commercial_id', '').strip() if not is_commercial else ''

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        if date_debut > date_fin:
            messages.error(request, 'La date de début doit être antérieure ou égale à la date de fin.')
            date_debut = today - timedelta(days=6)
            date_fin = today
            date_debut_str = date_debut.isoformat()
            date_fin_str = date_fin.isoformat()

        qs = BonRestitution.objects.select_related('user').filter(
            restitution_at__gte=date_debut, restitution_at__lte=date_fin,
        )

        if is_commercial:
            qs = qs.filter(user=request.user)
        elif commercial_id:
            qs = qs.filter(user_id=commercial_id)

        qs = qs.order_by('user__last_name', 'user__first_name', '-restitution_at')

        commerciaux_list = (
            None if is_commercial
            else User.objects.filter(groups__name='Commercial').order_by('last_name', 'first_name')
        )

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        rows = list(page_obj.object_list)
        for br in rows:
            br.erreur_suppression = _erreur_suppression_bon_restitution(br)

        return render(request, self.template_name, {
            'page_obj':             page_obj,
            'is_paginated':         page_obj.has_other_pages(),
            'bons_restitution':     rows,
            'is_commercial':        is_commercial,
            'date_debut':           date_debut_str,
            'date_fin':             date_fin_str,
            'commercial_id':        commercial_id,
            'commerciaux_list':     commerciaux_list,
        })


def _quantite_bons_sortie_non_soldes(commercial_id, ref_date):
    """Somme de quantite, groupée par produit, des BonSortieDetaille dont le
    BonSortie appartient à ce commercial, n'est pas encore soldé
    (historique_stock_initial IS NULL) et sortie_at <= ref_date."""
    return {
        row['produit_id']: row['total'] or 0
        for row in (
            BonSortieDetaille.objects
            .filter(
                bon_sortie__user_id=commercial_id,
                bon_sortie__historique_stock_initial__isnull=True,
                bon_sortie__sortie_at__lte=ref_date,
            )
            .values('produit_id').annotate(total=Sum('quantite'))
        )
    }


def _quantite_livree_non_soldee(commercial_id, ref_date):
    """Somme de quantite, groupée par produit, des BonLivraisonDetaille dont
    le BonLivraison appartient à ce commercial et dont le BonLivraisonCode
    n'est pas encore soldé (hist_stock_initial IS NULL) et bon_livraison_at
    <= ref_date — en ne retenant, pour chaque BonLivraisonCode, que son
    DERNIER BonLivraison (historique le plus récent), même principe que
    partout ailleurs dans l'application pour cette relation
    (BonLivraisonListView, etc.) — nécessaire pour ne pas compter deux fois
    un bon révisé."""
    dernier_bl_id_par_code = (
        BonLivraison.objects
        .filter(
            user_id=commercial_id,
            bon_livraison_code__bon_livraison_at__lte=ref_date,
            bon_livraison_code__hist_stock_initial__isnull=True,
        )
        .values('bon_livraison_code_id')
        .annotate(dernier_id=Max('id'))
        .values_list('dernier_id', flat=True)
    )
    return {
        row['produit_id']: row['total'] or 0
        for row in (
            BonLivraisonDetaille.objects
            .filter(bon_livraison_id__in=dernier_bl_id_par_code, produit__isnull=False)
            .values('produit_id').annotate(total=Sum('quantite'))
        )
    }


def _quantite_restituee_non_soldee(commercial_id, ref_date):
    """Somme de quantite, groupée par produit, des BonRestitutionDetaille
    dont le BonRestitution appartient à ce commercial, n'est pas encore
    soldé (historique_stock_initial IS NULL) et restitution_at <=
    ref_date."""
    return {
        row['produit_id']: row['total'] or 0
        for row in (
            BonRestitutionDetaille.objects
            .filter(
                bon_restitution__user_id=commercial_id,
                bon_restitution__historique_stock_initial__isnull=True,
                bon_restitution__restitution_at__lte=ref_date,
            )
            .values('produit_id').annotate(total=Sum('quantite'))
        )
    }


class BonRestitutionDefaultsView(GroupRequiredMixin, View):
    """Point d'entrée AJAX utilisé par le template Nouveau bon de
    restitution : étant donné un commercial (commercial_id) et une date de
    référence (restitution_at), calcule pour chaque produit concerné le
    « Reste calculé » = Requête 1 + Requête 2 − Requête 3 − Requête 4 :

    - Requête 1 (quantité initialement stockée) : somme de reel_stock,
      groupée par produit, des HistoriqueStockInitialDetaille du DERNIER
      HistoriqueStockInitial de ce commercial (indépendant de restitution_at) ;
    - Requête 2 (quantité des bons de sortie) : voir
      _quantite_bons_sortie_non_soldes ;
    - Requête 3 (quantité livrée) : voir _quantite_livree_non_soldee ;
    - Requête 4 (quantité déjà restituée) : voir
      _quantite_restituee_non_soldee.

    Le produit d'une ligne est retenu dès qu'il apparaît dans au moins une
    des quatre requêtes (union) ; une quantité absente d'une requête vaut 0
    pour ce produit."""
    group_required = ['Administration', 'Commercial']

    def get(self, request):
        commercial_id = request.GET.get('commercial_id', '').strip()
        restitution_at_str = request.GET.get('restitution_at', '').strip()

        if not commercial_id or not commercial_id.isdigit():
            return JsonResponse({'lignes': []})

        try:
            ref_date = date.fromisoformat(restitution_at_str)
        except ValueError:
            ref_date = date.today()

        # ── Requête 1 : dernier stock initial du commercial ─────────────────
        hist = (
            HistoriqueStockInitial.objects
            .filter(user_id=commercial_id)
            .order_by('-stock_initial_at')
            .first()
        )
        quantite_initiale = {}
        if hist is not None:
            quantite_initiale = {
                row['produit_id']: row['total'] or 0
                for row in (
                    hist.details.exclude(produit__isnull=True)
                    .values('produit_id').annotate(total=Sum('reel_stock'))
                )
            }

        quantite_sortie = _quantite_bons_sortie_non_soldes(commercial_id, ref_date)
        quantite_livree = _quantite_livree_non_soldee(commercial_id, ref_date)
        quantite_deja_restituee = _quantite_restituee_non_soldee(commercial_id, ref_date)

        produit_ids = (
            set(quantite_initiale) | set(quantite_sortie)
            | set(quantite_livree) | set(quantite_deja_restituee)
        )
        produits_par_id = {p.pk: p.nom for p in Produit.objects.filter(pk__in=produit_ids)}

        lignes = []
        for pid in produit_ids:
            qi = Decimal(quantite_initiale.get(pid, 0))
            qs = Decimal(quantite_sortie.get(pid, 0))
            ql = Decimal(quantite_livree.get(pid, 0))
            qr = Decimal(quantite_deja_restituee.get(pid, 0))
            lignes.append({
                'produit_id':              pid,
                'produit_nom':             produits_par_id.get(pid, '—'),
                'quantite_initiale':       str(qi),
                'quantite_sortie':         str(qs),
                'quantite_livree':         str(ql),
                'quantite_deja_restituee': str(qr),
                'reste_calcule':           str(qi + qs - ql - qr),
            })
        lignes.sort(key=lambda l: l['produit_nom'])

        return JsonResponse({'lignes': lignes})


class BonRestitutionCreateView(GroupRequiredMixin, View):
    """Formulaire « Nouveau bon de restitution » : Section 1 (Commercial
    verrouillé sur l'utilisateur connecté — champ masqué — s'il appartient
    au groupe Commercial, sinon liste déroulante des commerciaux actifs,
    « -Choisir- » ; Date, par défaut aujourd'hui, jamais postérieure à
    aujourd'hui). Section 2 est entièrement peuplée côté client via
    BonRestitutionDefaultsView (AJAX), déclenché au chargement (si le
    commercial est déjà connu) et à chaque changement de Commercial ou de
    Date — voir cette vue pour le détail du calcul du « Reste calculé » —
    et paginée côté client (8 lignes/page) pour ne jamais perdre les
    quantités déjà saisies en changeant de page. Section 3 permet d'ajouter
    librement d'autres produits (parmi les produits non semi-finis).

    Le bouton « Enregistrer tous » (voir post()) valide, dans l'ordre :
    Commercial défini (uniquement si le champ est visible) ; Date définie et
    <= aujourd'hui ; au moins une ligne valide (Produit et Quantité définis,
    Quantité > 0) entre la Section 2 et la Section 3. En cas de succès, dans
    une transaction.atomic() : création d'un BonRestitution
    (historique_stock_initial=Null) puis, pour les lignes valides des deux
    sections regroupées par produit_id (quantités sommées), création des
    BonRestitutionDetaille correspondants. En cas d'échec, la page est
    régénérée avec Commercial, Date et toutes les lignes de détail
    préservées (préremplissage JSON, comme pour achats/nouveau) — voir le
    template : les lignes préservées sont réaffichées telles quelles dans la
    Section 3, sans relancer le calcul AJAX de la Section 2, pour ne jamais
    perdre ni dupliquer une quantité saisie."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_restitution/create.html'

    def _context(self, request, *, commercial_id=None, restitution_at=None, detail_rows_json=None):
        is_commercial = _is_commercial(request.user)
        today = date.today()

        commerciaux_list = (
            None if is_commercial
            else User.objects.filter(groups__name='Commercial', is_active=True).order_by('last_name', 'first_name')
        )
        produits_list = Produit.objects.filter(is_produit_semi_fini=False).order_by('nom')

        if commercial_id is None:
            commercial_id = str(request.user.pk) if is_commercial else ''

        return {
            'is_commercial':    is_commercial,
            'commercial_id':    commercial_id,
            'commerciaux_list': commerciaux_list,
            'restitution_at':   restitution_at or today.isoformat(),
            'today':            today.isoformat(),
            'produits_json':    json.dumps([{'id': p.pk, 'nom': p.nom} for p in produits_list]),
            'detail_rows_json': detail_rows_json or '[]',
        }

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        is_commercial = _is_commercial(request.user)
        commercial_id = str(request.user.pk) if is_commercial else request.POST.get('commercial_id', '').strip()
        restitution_at_str = request.POST.get('restitution_at', '').strip()

        produit_ids = request.POST.getlist('detail_produit_id')
        quantite_strs = request.POST.getlist('detail_quantite_restituee')
        raw_detail_rows = [
            {'produit_id': p, 'quantite': q}
            for p, q in zip(produit_ids, quantite_strs)
        ]

        def _echec(message):
            messages.error(request, message)
            context = self._context(
                request,
                commercial_id=commercial_id,
                restitution_at=restitution_at_str,
                detail_rows_json=json.dumps(raw_detail_rows),
            )
            return render(request, self.template_name, context)

        if not is_commercial and (not commercial_id or not commercial_id.isdigit()):
            return _echec('Le commercial doit être défini.')

        if not restitution_at_str:
            return _echec('La date du bon de restitution doit être définie.')
        try:
            restitution_at = date.fromisoformat(restitution_at_str)
        except ValueError:
            return _echec('La date du bon de restitution doit être définie.')
        if restitution_at > date.today():
            return _echec('La date du bon de restitution ne peut pas être postérieure à la date du jour.')

        quantite_par_produit = {}
        for produit_id_s, quantite_s in zip(produit_ids, quantite_strs):
            produit_id_s = (produit_id_s or '').strip()
            quantite_s = (quantite_s or '').strip()
            if not produit_id_s or not produit_id_s.isdigit() or not quantite_s:
                continue
            try:
                quantite = Decimal(quantite_s)
            except InvalidOperation:
                continue
            if quantite <= 0:
                continue
            pid = int(produit_id_s)
            quantite_par_produit[pid] = quantite_par_produit.get(pid, Decimal('0')) + quantite

        if not quantite_par_produit:
            return _echec(
                'Il faut avoir au moins un enregistrement valide entre les sections '
                '« Produits à restituer » et « Ajouter d\'autres produits restitués ».'
            )

        try:
            with transaction.atomic():
                bon_restitution = BonRestitution.objects.create(
                    user_id=commercial_id,
                    restitution_at=restitution_at,
                    historique_stock_initial=None,
                )
                BonRestitutionDetaille.objects.bulk_create([
                    BonRestitutionDetaille(bon_restitution=bon_restitution, produit_id=pid, quantite=qte)
                    for pid, qte in quantite_par_produit.items()
                ])
        except Exception:
            return _echec("Échec de l'enregistrement du bon de restitution : aucune donnée n'a été modifiée.")

        log_audit(
            AuditAction.CREATE, f'Création bon de restitution {bon_restitution} — {len(quantite_par_produit)} produit(s)',
            table='BonRestitution', record_id=bon_restitution.pk, new_value=model_to_dict(bon_restitution),
        )
        messages.success(request, 'Bon de restitution enregistré avec succès.')
        return redirect('commercial:bon-restitution-list')


class BonRestitutionDetailPopupView(GroupRequiredMixin, View):
    """Contenu (fragment HTML, chargé en AJAX dans la pop-up « Détail bon de
    restitution ») déclenché par le bouton Visualiser de la liste des bons
    de restitution — Section 1 (Commercial, si l'utilisateur connecté n'est
    pas du groupe Commercial ; Date ; Soldé) et Section 2 (BonRestitutionDetaille
    du bon, Produit/Quantité, lecture seule)."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_restitution/detail_popup.html'

    def get(self, request, pk):
        bon_restitution = get_object_or_404(BonRestitution.objects.select_related('user'), pk=pk)
        return render(request, self.template_name, {
            'is_commercial':   _is_commercial(request.user),
            'bon_restitution': bon_restitution,
            'details':         bon_restitution.details.select_related('produit').order_by('pk'),
        })


class BonRestitutionDeleteView(GroupRequiredMixin, View):
    """Suppression d'un bon de restitution (voir
    _erreur_suppression_bon_restitution pour la condition bloquante — un bon
    déjà soldé ne peut être supprimé). Ordre de suppression : BonRestitutionDetaille
    puis BonRestitution, dans une transaction atomique. En cas d'échec (de
    validation ou d'écriture), rien n'est supprimé."""
    group_required = ['Administration', 'Commercial']

    def post(self, request, pk):
        bon_restitution = get_object_or_404(BonRestitution, pk=pk)
        reference = str(bon_restitution)

        erreur = _erreur_suppression_bon_restitution(bon_restitution)
        if erreur:
            messages.error(request, erreur)
            return redirect('commercial:bon-restitution-list')

        avant = {
            'bon_restitution': model_to_dict(bon_restitution),
            'details': [model_to_dict(d) for d in bon_restitution.details.all()],
        }

        try:
            with transaction.atomic():
                bon_restitution.details.all().delete()
                bon_restitution.delete()
        except Exception:
            messages.error(
                request,
                f"Échec de la suppression du bon de restitution {reference} : aucune donnée n'a été modifiée.",
            )
            return redirect('commercial:bon-restitution-list')

        log_audit(
            AuditAction.DELETE, f'Suppression bon de restitution {reference}',
            table='BonRestitution', record_id=pk, old_value=avant,
        )
        messages.success(request, f'Bon de restitution {reference} supprimé avec succès.')
        return redirect('commercial:bon-restitution-list')


class BonRestitutionUpdateView(GroupRequiredMixin, View):
    """Formulaire « Modification d'un bon de restitution » : Section 1
    (Commercial — visible et modifiable si l'utilisateur connecté n'est pas
    du groupe Commercial, choisi parmi les utilisateurs actifs dont l'Employe
    lié a service="Commercial" ; masqué et verrouillé sur soi-même sinon ;
    Date, modifiable, jamais postérieure à aujourd'hui). Section 2 : lignes
    de détail existantes (Produit modifiable parmi les produits non
    semi-finis, Quantité modifiable, bouton Supprimer) préremplies depuis la
    base. Section 3 : ajout libre d'autres produits, identique à Section 2
    (mêmes noms de champs — les deux sections sont fusionnées à
    l'enregistrement).

    Le bouton « Enregistrer les modifications » (voir post()) valide, dans
    l'ordre : le bon ne doit pas déjà être soldé (historique_stock_initial
    IS NULL, voir _erreur_suppression_bon_restitution) ; Commercial défini,
    actif, et lié à un Employe de service Commercial ; Date définie et <=
    aujourd'hui ; au moins une ligne valide (Produit et Quantité définis,
    Quantité > 0) entre la Section 2 et la Section 3. En cas de succès, dans
    une transaction.atomic() : mise à jour de BonRestitution (user,
    restitution_at), suppression de tous ses BonRestitutionDetaille puis
    recréation à partir des lignes valides des deux sections regroupées par
    produit_id (quantités sommées). En cas d'échec, la page est régénérée
    avec Commercial, Date et toutes les lignes de détail préservées
    (préremplissage JSON, comme pour achats/nouveau) — la Section 2 n'est
    alors pas rechargée depuis la base : toutes les lignes précédemment
    saisies sont réaffichées dans la Section 3, pour ne jamais en perdre ni
    en compter une en double."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_restitution/update.html'

    def _comptes_commerciaux_actifs(self):
        return (
            User.objects
            .filter(is_active=True, employes__service='Commercial')
            .distinct()
            .order_by('last_name', 'first_name')
        )

    def _context(
        self, request, bon_restitution, *,
        commercial_id=None, restitution_at=None, existing_rows_json=None, detail_rows_json=None,
    ):
        is_commercial = _is_commercial(request.user)
        today = date.today()

        if commercial_id is None:
            commercial_id = str(request.user.pk) if is_commercial else str(bon_restitution.user_id)

        if existing_rows_json is None and not detail_rows_json:
            existing_rows_json = json.dumps([
                {'produit_id': d.produit_id, 'quantite': str(d.quantite)}
                for d in bon_restitution.details.all().order_by('pk')
            ])

        produits_list = Produit.objects.filter(is_produit_semi_fini=False).order_by('nom')

        return {
            'bon_restitution':    bon_restitution,
            'is_commercial':      is_commercial,
            'commercial_id':      commercial_id,
            'commerciaux_list':   None if is_commercial else self._comptes_commerciaux_actifs(),
            'restitution_at':     restitution_at or bon_restitution.restitution_at.isoformat(),
            'today':              today.isoformat(),
            'produits_json':      json.dumps([{'id': p.pk, 'nom': p.nom} for p in produits_list]),
            'existing_rows_json': existing_rows_json or '[]',
            'detail_rows_json':   detail_rows_json or '[]',
        }

    def get(self, request, pk):
        bon_restitution = get_object_or_404(BonRestitution, pk=pk)
        return render(request, self.template_name, self._context(request, bon_restitution))

    def post(self, request, pk):
        bon_restitution = get_object_or_404(BonRestitution, pk=pk)
        is_commercial = _is_commercial(request.user)
        commercial_id = str(request.user.pk) if is_commercial else request.POST.get('commercial_id', '').strip()
        restitution_at_str = request.POST.get('restitution_at', '').strip()

        produit_ids = request.POST.getlist('detail_produit_id')
        quantite_strs = request.POST.getlist('detail_quantite')
        raw_detail_rows = [
            {'produit_id': p, 'quantite': q}
            for p, q in zip(produit_ids, quantite_strs)
        ]

        def _echec(message):
            messages.error(request, message)
            context = self._context(
                request, bon_restitution,
                commercial_id=commercial_id,
                restitution_at=restitution_at_str,
                existing_rows_json='[]',
                detail_rows_json=json.dumps(raw_detail_rows),
            )
            return render(request, self.template_name, context)

        if bon_restitution.historique_stock_initial_id is not None:
            return _echec('Un bon de restitution soldé ne peut être modifié.')

        if (
            not commercial_id or not commercial_id.isdigit()
            or not User.objects.filter(
                pk=commercial_id, is_active=True, employes__service='Commercial',
            ).exists()
        ):
            return _echec('Le bon de restitution doit être lié à un commercial actif.')

        try:
            restitution_at = date.fromisoformat(restitution_at_str)
        except ValueError:
            return _echec('Donner une date <= date du jour.')
        if restitution_at > date.today():
            return _echec('Donner une date <= date du jour.')

        quantite_par_produit = {}
        for produit_id_s, quantite_s in zip(produit_ids, quantite_strs):
            produit_id_s = (produit_id_s or '').strip()
            quantite_s = (quantite_s or '').strip()
            if not produit_id_s or not produit_id_s.isdigit() or not quantite_s:
                continue
            try:
                quantite = Decimal(quantite_s)
            except InvalidOperation:
                continue
            if quantite <= 0:
                continue
            pid = int(produit_id_s)
            quantite_par_produit[pid] = quantite_par_produit.get(pid, Decimal('0')) + quantite

        if not quantite_par_produit:
            return _echec('Il faut avoir au moins un enregistrement valide.')

        reference = str(bon_restitution)
        avant = {
            'bon_restitution': model_to_dict(bon_restitution),
            'details': [model_to_dict(d) for d in bon_restitution.details.all()],
        }

        try:
            with transaction.atomic():
                bon_restitution.user_id = commercial_id
                bon_restitution.restitution_at = restitution_at
                bon_restitution.save(update_fields=['user', 'restitution_at'])

                bon_restitution.details.all().delete()
                BonRestitutionDetaille.objects.bulk_create([
                    BonRestitutionDetaille(bon_restitution=bon_restitution, produit_id=pid, quantite=qte)
                    for pid, qte in quantite_par_produit.items()
                ])
        except Exception:
            return _echec(
                f"Échec de la modification du bon de restitution {reference} : aucune donnée n'a été modifiée.",
            )

        log_audit(
            AuditAction.UPDATE, f'Modification bon de restitution {reference} — {len(quantite_par_produit)} produit(s)',
            table='BonRestitution', record_id=bon_restitution.pk, old_value=avant,
            new_value=model_to_dict(bon_restitution),
        )
        messages.success(request, f'Bon de restitution {reference} modifié avec succès.')
        return redirect('commercial:bon-restitution-list')


class BonSortieDefaultsView(GroupRequiredMixin, View):
    """Point d'entrée AJAX utilisé par le template Nouveau bon de sortie :
    étant donné un commercial (commercial_id) et une date de référence
    (sortie_at), retourne les lignes du dernier BonSortie de ce commercial
    dont sortie_at tombe le même jour de la semaine que la date de
    référence — à défaut celles du dernier BonSortie de ce commercial tout
    court, à défaut aucune — pour préremplir la Section 2."""
    group_required = ['Administration', 'Commercial']

    def get(self, request):
        commercial_id = request.GET.get('commercial_id', '').strip()
        sortie_at_str = request.GET.get('sortie_at', '').strip()

        if not commercial_id or not commercial_id.isdigit():
            return JsonResponse({'sortie_lignes': []})

        try:
            ref_date = date.fromisoformat(sortie_at_str)
        except ValueError:
            ref_date = date.today()

        bons = list(
            BonSortie.objects.filter(user_id=commercial_id, sortie_at__isnull=False).only('id', 'sortie_at')
        )
        sortie_lignes = []
        if bons:
            meme_jour_semaine = [b for b in bons if b.sortie_at.weekday() == ref_date.weekday()]
            candidats = meme_jour_semaine or bons
            dernier = max(candidats, key=lambda b: (b.sortie_at, b.pk))
            sortie_lignes = [
                {'produit_id': d.produit_id, 'quantite': str(d.quantite)}
                for d in dernier.details.all().order_by('pk')
            ]

        return JsonResponse({'sortie_lignes': sortie_lignes})


class BonSortieCreateView(GroupRequiredMixin, View):
    """Formulaire « Nouveau bon de sortie » : Section 1 (Commercial verrouillé
    sur l'utilisateur connecté — champ masqué — s'il appartient au groupe
    Commercial, sinon liste déroulante « -Choisir- » ; Date, par défaut
    aujourd'hui, jamais postérieure à aujourd'hui). Sections 2 et 3 sont
    entièrement peuplées côté client via BonSortieDefaultsView (AJAX),
    déclenché au chargement (si le commercial est déjà connu) et à chaque
    changement de Commercial ou de Date — voir cette vue pour le détail des
    règles de préremplissage. La modification du stock initial (bouton
    « Modifier le stock initial du commercial ») sera développée
    ultérieurement — ce bouton est pour l'instant inerte.

    Le bouton « Enregistrer tous » (voir post()) valide, dans l'ordre :
    Commercial défini ; Date définie et <= aujourd'hui ; au moins une ligne
    valide en Section 3 (Produit et Quantité définis, Quantité > 0). En cas
    de succès, dans une transaction.atomic() : création d'un BonSortie
    (historique_stock_initial=Null) puis, pour les lignes valides regroupées
    par produit_id (quantités sommées), création des BonSortieDetaille
    correspondants. En cas d'échec, la page est régénérée avec Commercial,
    Date et les lignes de détail préservés (préremplissage JSON, comme pour
    achats/nouveau)."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_sortie/create.html'

    def _context(self, request, *, commercial_id=None, sortie_at=None, detail_rows_json=None):
        is_commercial = _is_commercial(request.user)
        today = date.today()

        commerciaux_list = (
            None if is_commercial
            else User.objects.filter(groups__name='Commercial').order_by('last_name', 'first_name')
        )
        produits_list = Produit.objects.order_by('nom')

        if commercial_id is None:
            commercial_id = str(request.user.pk) if is_commercial else ''

        return {
            'is_commercial':    is_commercial,
            'commercial_id':    commercial_id,
            'commerciaux_list': commerciaux_list,
            'sortie_at':        sortie_at or today.isoformat(),
            'today':            today.isoformat(),
            'produits_json':    json.dumps([{'id': p.pk, 'nom': p.nom} for p in produits_list]),
            'detail_rows_json': detail_rows_json or '[]',
        }

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        is_commercial = _is_commercial(request.user)
        commercial_id = str(request.user.pk) if is_commercial else request.POST.get('commercial_id', '').strip()
        sortie_at_str = request.POST.get('sortie_at', '').strip()

        produit_ids = request.POST.getlist('detail_produit_id')
        quantite_strs = request.POST.getlist('detail_quantite')
        raw_detail_rows = [
            {'produit_id': p, 'quantite': q}
            for p, q in zip(produit_ids, quantite_strs)
        ]

        def _echec(message):
            messages.error(request, message)
            context = self._context(
                request,
                commercial_id=commercial_id,
                sortie_at=sortie_at_str,
                detail_rows_json=json.dumps(raw_detail_rows),
            )
            return render(request, self.template_name, context)

        if not commercial_id or not commercial_id.isdigit():
            return _echec('il faut définir le commercial')

        try:
            sortie_at = date.fromisoformat(sortie_at_str)
        except ValueError:
            return _echec('il faut définir la date du bon de sortie')
        if sortie_at > date.today():
            return _echec('il faut définir la date du bon de sortie')

        quantite_par_produit = {}
        for produit_id_s, quantite_s in zip(produit_ids, quantite_strs):
            produit_id_s = (produit_id_s or '').strip()
            quantite_s = (quantite_s or '').strip()
            if not produit_id_s or not produit_id_s.isdigit() or not quantite_s:
                continue
            try:
                quantite = Decimal(quantite_s)
            except InvalidOperation:
                continue
            if quantite <= 0:
                continue
            pid = int(produit_id_s)
            quantite_par_produit[pid] = quantite_par_produit.get(pid, Decimal('0')) + quantite

        if not quantite_par_produit:
            return _echec(
                'il faut avoir au moins un enregistrement valide dans la section '
                '"Liste par défaut des produits nouvellement sorties"'
            )

        try:
            with transaction.atomic():
                bon_sortie = BonSortie.objects.create(
                    user_id=commercial_id,
                    sortie_at=sortie_at,
                    historique_stock_initial=None,
                )
                BonSortieDetaille.objects.bulk_create([
                    BonSortieDetaille(bon_sortie=bon_sortie, produit_id=pid, quantite=qte)
                    for pid, qte in quantite_par_produit.items()
                ])
        except Exception:
            return _echec("Échec de l'enregistrement du bon de sortie : aucune donnée n'a été modifiée.")

        log_audit(
            AuditAction.CREATE, f'Création bon de sortie {bon_sortie} — {len(quantite_par_produit)} produit(s)',
            table='BonSortie', record_id=bon_sortie.pk, new_value=model_to_dict(bon_sortie),
        )
        messages.success(request, 'Bon de sortie enregistré avec succès.')
        return redirect('commercial:bon-sortie-list')


class BonSortieDetailPopupView(GroupRequiredMixin, View):
    """Contenu (fragment HTML, chargé en AJAX dans la pop-up « Détail bon de
    sortie ») déclenché par le bouton Visualiser de la liste des bons de
    sortie — Section 1 (Commercial, si l'utilisateur connecté n'est pas du
    groupe Commercial ; Date ; Soldé) et Section 2 (BonSortieDetaille du bon,
    Produit/Quantité, lecture seule)."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_sortie/detail_popup.html'

    def get(self, request, pk):
        bon_sortie = get_object_or_404(BonSortie.objects.select_related('user'), pk=pk)
        return render(request, self.template_name, {
            'is_commercial': _is_commercial(request.user),
            'bon_sortie':    bon_sortie,
            'details':       bon_sortie.details.select_related('produit').order_by('pk'),
        })


class BonSortieDeleteView(GroupRequiredMixin, View):
    """Suppression d'un bon de sortie (voir _erreur_suppression_bon_sortie
    pour la condition bloquante — un bon déjà soldé ne peut être supprimé).
    Ordre de suppression : BonSortieDetaille puis BonSortie, dans une
    transaction atomique. En cas d'échec (de validation ou d'écriture),
    rien n'est supprimé."""
    group_required = ['Administration', 'Commercial']

    def post(self, request, pk):
        bon_sortie = get_object_or_404(BonSortie, pk=pk)
        reference = str(bon_sortie)

        erreur = _erreur_suppression_bon_sortie(bon_sortie)
        if erreur:
            messages.error(request, erreur)
            return redirect('commercial:bon-sortie-list')

        avant = {
            'bon_sortie': model_to_dict(bon_sortie),
            'details': [model_to_dict(d) for d in bon_sortie.details.all()],
        }

        try:
            with transaction.atomic():
                bon_sortie.details.all().delete()
                bon_sortie.delete()
        except Exception:
            messages.error(
                request,
                f"Échec de la suppression du bon de sortie {reference} : aucune donnée n'a été modifiée.",
            )
            return redirect('commercial:bon-sortie-list')

        log_audit(
            AuditAction.DELETE, f'Suppression bon de sortie {reference}',
            table='BonSortie', record_id=pk, old_value=avant,
        )
        messages.success(request, f'Bon de sortie {reference} supprimé avec succès.')
        return redirect('commercial:bon-sortie-list')


class BonSortieUpdateView(GroupRequiredMixin, View):
    """Formulaire « Modification d'un bon de sortie » : Section 1
    (Commercial — visible et modifiable si l'utilisateur connecté n'est pas
    du groupe Commercial, choisi parmi les utilisateurs actifs dont l'Employe
    lié a service="Commercial" ; masqué et verrouillé sur soi-même sinon ;
    Date, modifiable, jamais postérieure à aujourd'hui). Section 2 : lignes
    de détail existantes (Produit modifiable parmi les produits non
    semi-finis, Quantité modifiable, bouton Supprimer) préremplies depuis la
    base. Section 3 : ajout libre d'autres produits, identique à Section 2
    (mêmes noms de champs — les deux sections sont fusionnées à
    l'enregistrement).

    Le bouton « Enregistrer les modifications » (voir post()) valide, dans
    l'ordre : le bon ne doit pas déjà être soldé (historique_stock_initial
    IS NULL, voir _erreur_suppression_bon_sortie) ; Commercial défini, actif,
    et lié à un Employe de service Commercial ; Date définie et <=
    aujourd'hui ; au moins une ligne valide (Produit et Quantité définis,
    Quantité > 0) entre la Section 2 et la Section 3. En cas de succès, dans
    une transaction.atomic() : mise à jour de BonSortie (user, sortie_at),
    suppression de tous ses BonSortieDetaille puis recréation à partir des
    lignes valides des deux sections regroupées par produit_id (quantités
    sommées). En cas d'échec, la page est régénérée avec Commercial, Date
    et toutes les lignes de détail préservées (préremplissage JSON, comme
    pour achats/nouveau) — la Section 2 n'est alors pas rechargée depuis la
    base : toutes les lignes précédemment saisies sont réaffichées dans la
    Section 3, pour ne jamais en perdre ni en compter une en double."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/bon_sortie/update.html'

    def _comptes_commerciaux_actifs(self):
        return (
            User.objects
            .filter(is_active=True, employes__service='Commercial')
            .distinct()
            .order_by('last_name', 'first_name')
        )

    def _context(
        self, request, bon_sortie, *,
        commercial_id=None, sortie_at=None, existing_rows_json=None, detail_rows_json=None,
    ):
        is_commercial = _is_commercial(request.user)
        today = date.today()

        if commercial_id is None:
            commercial_id = str(request.user.pk) if is_commercial else str(bon_sortie.user_id)

        if existing_rows_json is None and not detail_rows_json:
            existing_rows_json = json.dumps([
                {'produit_id': d.produit_id, 'quantite': str(d.quantite)}
                for d in bon_sortie.details.all().order_by('pk')
            ])

        produits_list = Produit.objects.filter(is_produit_semi_fini=False).order_by('nom')

        return {
            'bon_sortie':         bon_sortie,
            'is_commercial':      is_commercial,
            'commercial_id':      commercial_id,
            'commerciaux_list':   None if is_commercial else self._comptes_commerciaux_actifs(),
            'sortie_at':          sortie_at or bon_sortie.sortie_at.isoformat(),
            'today':              today.isoformat(),
            'produits_json':      json.dumps([{'id': p.pk, 'nom': p.nom} for p in produits_list]),
            'existing_rows_json': existing_rows_json or '[]',
            'detail_rows_json':   detail_rows_json or '[]',
        }

    def get(self, request, pk):
        bon_sortie = get_object_or_404(BonSortie, pk=pk)
        return render(request, self.template_name, self._context(request, bon_sortie))

    def post(self, request, pk):
        bon_sortie = get_object_or_404(BonSortie, pk=pk)
        is_commercial = _is_commercial(request.user)
        commercial_id = str(request.user.pk) if is_commercial else request.POST.get('commercial_id', '').strip()
        sortie_at_str = request.POST.get('sortie_at', '').strip()

        produit_ids = request.POST.getlist('detail_produit_id')
        quantite_strs = request.POST.getlist('detail_quantite')
        raw_detail_rows = [
            {'produit_id': p, 'quantite': q}
            for p, q in zip(produit_ids, quantite_strs)
        ]

        def _echec(message):
            messages.error(request, message)
            context = self._context(
                request, bon_sortie,
                commercial_id=commercial_id,
                sortie_at=sortie_at_str,
                existing_rows_json='[]',
                detail_rows_json=json.dumps(raw_detail_rows),
            )
            return render(request, self.template_name, context)

        if bon_sortie.historique_stock_initial_id is not None:
            return _echec('Un bon de sortie soldé ne peut être modifié.')

        if (
            not commercial_id or not commercial_id.isdigit()
            or not User.objects.filter(
                pk=commercial_id, is_active=True, employes__service='Commercial',
            ).exists()
        ):
            return _echec('Le bon de sortie doit être lié à un commercial actif.')

        try:
            sortie_at = date.fromisoformat(sortie_at_str)
        except ValueError:
            return _echec('Donner une date <= date du jour.')
        if sortie_at > date.today():
            return _echec('Donner une date <= date du jour.')

        quantite_par_produit = {}
        for produit_id_s, quantite_s in zip(produit_ids, quantite_strs):
            produit_id_s = (produit_id_s or '').strip()
            quantite_s = (quantite_s or '').strip()
            if not produit_id_s or not produit_id_s.isdigit() or not quantite_s:
                continue
            try:
                quantite = Decimal(quantite_s)
            except InvalidOperation:
                continue
            if quantite <= 0:
                continue
            pid = int(produit_id_s)
            quantite_par_produit[pid] = quantite_par_produit.get(pid, Decimal('0')) + quantite

        if not quantite_par_produit:
            return _echec('Il faut avoir au moins un enregistrement valide.')

        reference = str(bon_sortie)
        avant = {
            'bon_sortie': model_to_dict(bon_sortie),
            'details': [model_to_dict(d) for d in bon_sortie.details.all()],
        }

        try:
            with transaction.atomic():
                bon_sortie.user_id = commercial_id
                bon_sortie.sortie_at = sortie_at
                bon_sortie.save(update_fields=['user', 'sortie_at'])

                bon_sortie.details.all().delete()
                BonSortieDetaille.objects.bulk_create([
                    BonSortieDetaille(bon_sortie=bon_sortie, produit_id=pid, quantite=qte)
                    for pid, qte in quantite_par_produit.items()
                ])
        except Exception:
            return _echec(
                f"Échec de la modification du bon de sortie {reference} : aucune donnée n'a été modifiée.",
            )

        log_audit(
            AuditAction.UPDATE, f'Modification bon de sortie {reference} — {len(quantite_par_produit)} produit(s)',
            table='BonSortie', record_id=bon_sortie.pk, old_value=avant,
            new_value=model_to_dict(bon_sortie),
        )
        messages.success(request, f'Bon de sortie {reference} modifié avec succès.')
        return redirect('commercial:bon-sortie-list')


# ─── Historique stock initial (mises à jour des stocks commerciaux) ──────────

class HistoriqueStockInitialListView(GroupRequiredMixin, View):
    """Liste des HistoriqueStockInitial (commercial.models) sur une période —
    filtrée par commercial : verrouillée sur l'utilisateur connecté (champ
    masqué) s'il appartient au groupe Commercial, sinon un filtre déroulant
    (par défaut « tous les commerciaux », limité aux utilisateurs actifs
    dont l'Employe lié a service="Commercial") est proposé. Triée par date
    de mise à jour décroissante, paginée à 10 lignes. « Visualiser » et
    « + Nouvelle mise à jour » sont des actions à développer ultérieurement
    (placeholders)."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/historique_stock_initial/list.html'
    PAGINATE_BY = 10

    def get(self, request):
        is_commercial = _is_commercial(request.user)
        today = date.today()

        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str = request.GET.get('date_fin', '').strip()
        commercial_id = request.GET.get('commercial_id', '').strip() if not is_commercial else ''

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        if date_debut > date_fin:
            messages.error(request, 'La date de début doit être antérieure ou égale à la date de fin.')
            date_debut = today - timedelta(days=6)
            date_fin = today
            date_debut_str = date_debut.isoformat()
            date_fin_str = date_fin.isoformat()

        qs = HistoriqueStockInitial.objects.select_related('user').filter(
            stock_initial_at__gte=date_debut, stock_initial_at__lte=date_fin,
        )

        if is_commercial:
            qs = qs.filter(user=request.user)
        elif commercial_id:
            qs = qs.filter(user_id=commercial_id)

        commerciaux_list = (
            None if is_commercial
            else User.objects.filter(
                is_active=True, employes__service='Commercial',
            ).distinct().order_by('last_name', 'first_name')
        )

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':         page_obj,
            'is_paginated':     page_obj.has_other_pages(),
            'historiques':      page_obj.object_list,
            'is_commercial':    is_commercial,
            'date_debut':       date_debut_str,
            'date_fin':         date_fin_str,
            'commercial_id':    commercial_id,
            'commerciaux_list': commerciaux_list,
        })


class HistoriqueStockInitialDefaultsView(GroupRequiredMixin, View):
    """Point d'entrée AJAX utilisé par le template Mise à jour des stocks de
    produits chez le commercial : étant donné un commercial (commercial_id)
    et une date de référence (stock_initial_at), calcule pour chaque produit
    concerné le « Stock calculé » = Requête 1 + Requête 2 − Requête 3 −
    Requête 4 :

    - Requête 1 (quantité initialement stockée) : somme de reel_stock,
      groupée par produit, du DERNIER HistoriqueStockInitial de ce
      commercial dont stock_initial_at <= la date de référence (à la
      différence de BonRestitutionDefaultsView, ici la date de référence
      borne aussi la Requête 1 — on ne doit jamais tenir compte d'un stock
      initial postérieur à la date de mise à jour en cours) ;
    - Requête 2 (quantité des bons de sortie) : voir
      _quantite_bons_sortie_non_soldes ;
    - Requête 3 (quantité livrée) : voir _quantite_livree_non_soldee ;
    - Requête 4 (quantité déjà restituée) : voir
      _quantite_restituee_non_soldee.

    Le produit d'une ligne est retenu dès qu'il apparaît dans au moins une
    des quatre requêtes (union) ; une quantité absente d'une requête vaut 0
    pour ce produit."""
    group_required = ['Administration', 'Commercial']

    def get(self, request):
        commercial_id = request.GET.get('commercial_id', '').strip()
        stock_initial_at_str = request.GET.get('stock_initial_at', '').strip()

        if not commercial_id or not commercial_id.isdigit():
            return JsonResponse({'lignes': []})

        try:
            ref_date = date.fromisoformat(stock_initial_at_str)
        except ValueError:
            ref_date = date.today()

        # ── Requête 1 : dernier stock initial du commercial <= la date ──────
        hist = (
            HistoriqueStockInitial.objects
            .filter(user_id=commercial_id, stock_initial_at__lte=ref_date)
            .order_by('-stock_initial_at')
            .first()
        )
        quantite_initiale = {}
        if hist is not None:
            quantite_initiale = {
                row['produit_id']: row['total'] or 0
                for row in (
                    hist.details.exclude(produit__isnull=True)
                    .values('produit_id').annotate(total=Sum('reel_stock'))
                )
            }

        quantite_sortie = _quantite_bons_sortie_non_soldes(commercial_id, ref_date)
        quantite_livree = _quantite_livree_non_soldee(commercial_id, ref_date)
        quantite_deja_restituee = _quantite_restituee_non_soldee(commercial_id, ref_date)

        produit_ids = (
            set(quantite_initiale) | set(quantite_sortie)
            | set(quantite_livree) | set(quantite_deja_restituee)
        )
        produits_par_id = {p.pk: p.nom for p in Produit.objects.filter(pk__in=produit_ids)}

        lignes = []
        for pid in produit_ids:
            qi = Decimal(quantite_initiale.get(pid, 0))
            qs = Decimal(quantite_sortie.get(pid, 0))
            ql = Decimal(quantite_livree.get(pid, 0))
            qr = Decimal(quantite_deja_restituee.get(pid, 0))
            lignes.append({
                'produit_id':              pid,
                'produit_nom':             produits_par_id.get(pid, '—'),
                'quantite_initiale':       str(qi),
                'quantite_sortie':         str(qs),
                'quantite_livree':         str(ql),
                'quantite_deja_restituee': str(qr),
                'calcul_stock':            str(qi + qs - ql - qr),
            })
        lignes.sort(key=lambda l: l['produit_nom'])

        return JsonResponse({'lignes': lignes})


class HistoriqueStockInitialCreateView(GroupRequiredMixin, View):
    """Formulaire « Mise à jour des stocks de produits chez le commercial » :
    Section 1 (Commercial — verrouillé sur l'utilisateur connecté, champ
    masqué, s'il appartient au groupe Commercial, sinon liste déroulante des
    commerciaux actifs dont l'Employe lié a service="Commercial",
    « -Choisir- » ; Date de mise à jour, par défaut aujourd'hui, jamais
    postérieure à aujourd'hui). Section 2 entièrement peuplée côté client
    via HistoriqueStockInitialDefaultsView (AJAX), déclenché au chargement
    (si le commercial est déjà connu) et à chaque changement de Commercial
    ou de Date — voir cette vue pour le détail du calcul du « Stock
    calculé » — et paginée côté client (8 lignes/page) pour ne jamais
    perdre les valeurs déjà saisies en changeant de page. Section 3 permet
    d'ajouter librement d'autres produits (parmi les produits non
    semi-finis), avec un « Stock calculé » toujours à 0 (verrouillé,
    puisqu'un produit ajouté manuellement n'est par définition sujet
    d'aucune des quatre requêtes).

    Le bouton « Enregistrer » (voir post()) valide, dans l'ordre :
    Commercial défini (et existant) ; Date de mise à jour définie, <=
    aujourd'hui et >= la stock_initial_at du dernier HistoriqueStockInitial
    de ce commercial s'il existe ; au moins une ligne valide (Produit et
    Stock réel définis, Stock réel > 0) entre la Section 2 et la Section 3.
    En cas de succès, dans une transaction.atomic() : création d'un
    HistoriqueStockInitial, puis, pour CHAQUE ligne valide des deux sections
    (sans regroupement — contrairement aux bons de sortie/restitution,
    calcul_stock/reel_stock/observation ne sont pas des quantités
    sommables), création du HistoriqueStockInitialDetaille correspondant.
    Les BonSortie, BonLivraisonCode et BonRestitution de ce commercial non
    encore soldés et dont la date <= la date de mise à jour sont ensuite
    marqués soldés (leur FK vers HistoriqueStockInitial pointe vers ce
    nouvel enregistrement). En cas d'échec, la page est régénérée avec
    Commercial, Date et toutes les lignes de détail préservées
    (préremplissage JSON, comme pour achats/nouveau) — la Section 2 n'est
    alors pas rechargée depuis les requêtes : toutes les lignes précédemment
    saisies sont réaffichées dans la Section 3, pour ne jamais en perdre ni
    en compter une en double."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/historique_stock_initial/create.html'

    def _context(
        self, request, *,
        commercial_id=None, stock_initial_at=None, detail_rows_json=None,
    ):
        is_commercial = _is_commercial(request.user)
        today = date.today()

        commerciaux_list = (
            None if is_commercial
            else User.objects.filter(
                is_active=True, employes__service='Commercial',
            ).distinct().order_by('last_name', 'first_name')
        )
        produits_list = Produit.objects.filter(is_produit_semi_fini=False).order_by('nom')

        if commercial_id is None:
            commercial_id = str(request.user.pk) if is_commercial else ''

        return {
            'is_commercial':     is_commercial,
            'commercial_id':     commercial_id,
            'commerciaux_list':  commerciaux_list,
            'stock_initial_at':  stock_initial_at or today.isoformat(),
            'today':             today.isoformat(),
            'produits_json':     json.dumps([{'id': p.pk, 'nom': p.nom} for p in produits_list]),
            'detail_rows_json':  detail_rows_json or '[]',
        }

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        is_commercial = _is_commercial(request.user)
        commercial_id = str(request.user.pk) if is_commercial else request.POST.get('commercial_id', '').strip()
        stock_initial_at_str = request.POST.get('stock_initial_at', '').strip()

        produit_ids = request.POST.getlist('detail_produit_id')
        reel_stock_strs = request.POST.getlist('detail_reel_stock')
        calcul_stock_strs = request.POST.getlist('detail_calcul_stock')
        observations = request.POST.getlist('detail_observation')

        raw_detail_rows = [
            {'produit_id': p, 'reel_stock': r, 'calcul_stock': c, 'observation': o}
            for p, r, c, o in zip(produit_ids, reel_stock_strs, calcul_stock_strs, observations)
        ]

        def _echec(message):
            messages.error(request, message)
            context = self._context(
                request,
                commercial_id=commercial_id,
                stock_initial_at=stock_initial_at_str,
                detail_rows_json=json.dumps(raw_detail_rows),
            )
            return render(request, self.template_name, context)

        if not commercial_id or not commercial_id.isdigit() or not User.objects.filter(pk=commercial_id).exists():
            return _echec('Le commercial doit être défini.')

        try:
            stock_initial_at = date.fromisoformat(stock_initial_at_str)
        except ValueError:
            return _echec('La date de mise à jour doit être définie.')
        if stock_initial_at > date.today():
            return _echec('La date de mise à jour ne peut pas être postérieure à la date du jour.')

        dernier_hist = (
            HistoriqueStockInitial.objects.filter(user_id=commercial_id).order_by('-stock_initial_at').first()
        )
        if dernier_hist is not None and stock_initial_at < dernier_hist.stock_initial_at:
            return _echec(
                'La date de mise à jour ne peut pas être antérieure à la dernière mise à jour de stock de ce '
                f'commercial ({dernier_hist.stock_initial_at.strftime("%d/%m/%Y")}).'
            )

        lignes_valides = []
        for produit_id_s, reel_stock_s, calcul_stock_s, observation in zip(
            produit_ids, reel_stock_strs, calcul_stock_strs, observations,
        ):
            produit_id_s = (produit_id_s or '').strip()
            reel_stock_s = (reel_stock_s or '').strip()
            if not produit_id_s or not produit_id_s.isdigit() or not reel_stock_s:
                continue
            try:
                reel_stock = Decimal(reel_stock_s)
            except InvalidOperation:
                continue
            if reel_stock <= 0:
                continue
            try:
                calcul_stock = Decimal(calcul_stock_s) if calcul_stock_s else Decimal('0')
            except InvalidOperation:
                calcul_stock = Decimal('0')
            lignes_valides.append({
                'produit_id':   int(produit_id_s),
                'reel_stock':   int(reel_stock),
                'calcul_stock': int(calcul_stock),
                'observation':  (observation or '').strip() or None,
            })

        if not lignes_valides:
            return _echec(
                'Il faut avoir au moins un enregistrement valide entre les sections '
                '« Stocks à mettre à jour » et « Ajouter d\'autres produits ».'
            )

        try:
            with transaction.atomic():
                nouveau_hist = HistoriqueStockInitial.objects.create(
                    user_id=commercial_id, stock_initial_at=stock_initial_at,
                )
                HistoriqueStockInitialDetaille.objects.bulk_create([
                    HistoriqueStockInitialDetaille(
                        historique_stock_initial=nouveau_hist,
                        produit_id=l['produit_id'],
                        calcul_stock=l['calcul_stock'],
                        reel_stock=l['reel_stock'],
                        observation=l['observation'],
                    )
                    for l in lignes_valides
                ])

                BonSortie.objects.filter(
                    user_id=commercial_id, sortie_at__lte=stock_initial_at,
                    historique_stock_initial__isnull=True,
                ).update(historique_stock_initial=nouveau_hist)

                code_ids = list(
                    BonLivraisonCode.objects.filter(
                        bons_livraison__user_id=commercial_id,
                        bon_livraison_at__lte=stock_initial_at,
                        hist_stock_initial__isnull=True,
                    ).values_list('pk', flat=True).distinct()
                )
                BonLivraisonCode.objects.filter(pk__in=code_ids).update(hist_stock_initial=nouveau_hist)

                BonRestitution.objects.filter(
                    user_id=commercial_id, restitution_at__lte=stock_initial_at,
                    historique_stock_initial__isnull=True,
                ).update(historique_stock_initial=nouveau_hist)
        except Exception:
            return _echec(
                "Échec de l'enregistrement de la mise à jour du stock : aucune donnée n'a été modifiée.",
            )

        log_audit(
            AuditAction.CREATE, f'Création mise à jour stock {nouveau_hist} — {len(lignes_valides)} produit(s)',
            table='HistoriqueStockInitial', record_id=nouveau_hist.pk, new_value=model_to_dict(nouveau_hist),
        )
        messages.success(request, 'Mise à jour du stock enregistrée avec succès.')
        return redirect('commercial:historique-stock-initial-list')


# ─── Fournisseurs ─────────────────────────────────────────────────────────────

class FournisseurListView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/fournisseur/list.html'
    PAGINATE_BY = 5

    def get(self, request):
        raison_sociale_id = request.GET.get('raison_sociale', '').strip()
        type_fournisseur  = request.GET.get('type_fournisseur', '').strip()
        nom_contact       = request.GET.get('nom_contact', '').strip()

        qs = Fournisseur.objects.order_by('raison_sociale')
        if raison_sociale_id:
            qs = qs.filter(pk=raison_sociale_id)
        if type_fournisseur:
            qs = qs.filter(type_fournisseur=type_fournisseur)
        if nom_contact:
            qs = qs.filter(nom_contact__icontains=nom_contact)

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj  = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':          page_obj,
            'is_paginated':      page_obj.has_other_pages(),
            'fournisseurs':      page_obj.object_list,
            'raison_sociale_id': raison_sociale_id,
            'type_fournisseur':  type_fournisseur,
            'nom_contact':       nom_contact,
            'fournisseurs_list': Fournisseur.objects.order_by('raison_sociale'),
            'type_choices':      Fournisseur.TYPE_CHOICES,
            'is_administration': _is_administration(request.user),
        })


class FournisseurCreateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/fournisseur/create.html'

    def get(self, request):
        return render(request, self.template_name, {'form': FournisseurForm()})

    def post(self, request):
        form = FournisseurForm(request.POST)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return render(request, self.template_name, {'form': form})

        fournisseur = form.save(commit=False)
        if not fournisseur.type_fournisseur:
            fournisseur.type_fournisseur = 'Divers'
        fournisseur.save()

        log_audit(
            AuditAction.CREATE, f'Création fournisseur « {fournisseur.raison_sociale} »',
            table='Fournisseur', record_id=fournisseur.pk, new_value=model_to_dict(fournisseur),
        )
        messages.success(request, f'Fournisseur « {fournisseur.raison_sociale} » ajouté avec succès.')
        return redirect('commercial:fournisseur-list')


class FournisseurDetailView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/fournisseur/detail.html'

    def get(self, request, pk):
        fournisseur = get_object_or_404(Fournisseur, pk=pk)
        return render(request, self.template_name, {'fournisseur': fournisseur})


class FournisseurUpdateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/fournisseur/update.html'

    def get(self, request, pk):
        fournisseur = get_object_or_404(Fournisseur, pk=pk)
        form = FournisseurForm(instance=fournisseur)
        return render(request, self.template_name, {'fournisseur': fournisseur, 'form': form})

    def post(self, request, pk):
        fournisseur = get_object_or_404(Fournisseur, pk=pk)
        avant = model_to_dict(fournisseur)
        form = FournisseurForm(request.POST, instance=fournisseur)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return render(request, self.template_name, {'fournisseur': fournisseur, 'form': form})

        fournisseur = form.save(commit=False)
        if not fournisseur.type_fournisseur:
            fournisseur.type_fournisseur = 'Divers'
        fournisseur.save()

        log_audit(
            AuditAction.UPDATE, f'Modification fournisseur « {fournisseur.raison_sociale} »',
            table='Fournisseur', record_id=fournisseur.pk, old_value=avant, new_value=model_to_dict(fournisseur),
        )
        messages.success(request, f'Fournisseur « {fournisseur.raison_sociale} » modifié avec succès.')
        return redirect('commercial:fournisseur-list')


class FournisseurDeleteView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']

    def post(self, request, pk):
        fournisseur = get_object_or_404(Fournisseur, pk=pk)
        nom = fournisseur.raison_sociale

        if not _is_administration(request.user):
            messages.error(
                request,
                'Seuls les utilisateurs du groupe Administration peuvent supprimer un fournisseur.',
            )
            return redirect('commercial:fournisseur-list')

        avant = model_to_dict(fournisseur)
        try:
            fournisseur.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer « {nom} » : cet enregistrement est encore lié à "
                "d'autres tables (achats, dépenses…).",
            )
        else:
            log_audit(
                AuditAction.DELETE, f'Suppression fournisseur « {nom} »',
                table='Fournisseur', record_id=pk, old_value=avant,
            )
            messages.success(request, f'Fournisseur « {nom} » supprimé avec succès.')
        return redirect('commercial:fournisseur-list')


class AchatListView(GroupRequiredMixin, View):
    group_required = 'Commercial'
    template_name = 'commercial/achat/list.html'
    PAGINATE_BY = 25

    def get(self, request):
        today = date.today()

        # ── Lecture des filtres GET ───────────────────────────────
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str   = request.GET.get('date_fin',   '').strip()
        fournisseur_id = request.GET.get('fournisseur_id', '').strip()
        statut         = request.GET.get('statut', '').strip()

        # Valeurs par défaut : 7 derniers jours
        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        if date_debut > date_fin:
            messages.error(request, 'La date de début doit être antérieure ou égale à la date de fin.')
            date_debut = today - timedelta(days=6)
            date_fin = today
            date_debut_str = date_debut.isoformat()
            date_fin_str = date_fin.isoformat()

        # ── Queryset filtré ────────────────────────────────────────
        # La table Achat est un historique (voir AchatUpdateView) : un même
        # AchatCode peut avoir plusieurs Achat. On affiche tous les AchatCode
        # de la période, mais chacun représenté uniquement par son DERNIER
        # Achat (celui dont l'id est le plus grand pour ce achat_code_id).
        dernier_achat_id_par_code = (
            Achat.objects
            .filter(achat_code__achat_at__gte=date_debut, achat_code__achat_at__lte=date_fin)
            .values('achat_code_id')
            .annotate(dernier_id=Max('id'))
            .values_list('dernier_id', flat=True)
        )
        qs = Achat.objects.select_related('achat_code__fournisseur').filter(pk__in=dernier_achat_id_par_code)
        if fournisseur_id:
            qs = qs.filter(achat_code__fournisseur_id=fournisseur_id)
        if statut:
            qs = qs.filter(statut=statut)

        qs = qs.order_by('achat_code__achat_at', 'pk')

        # ── Données pour les listes déroulantes ───────────────────
        fournisseurs_list = (
            Fournisseur.objects
            .filter(type_fournisseur=FOURNISSEUR_TYPE_ACHAT)
            .order_by('raison_sociale')
        )

        # ── Pagination ────────────────────────────────────────────
        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj  = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':          page_obj,
            'is_paginated':      page_obj.has_other_pages(),
            'date_debut':        date_debut_str,
            'date_fin':          date_fin_str,
            'fournisseur_id':    fournisseur_id,
            'statut':            statut,
            'fournisseurs_list': fournisseurs_list,
            'statut_choices':    STATUT_PAIEMENT_CHOICES,
            'total_count':       paginator.count,
        })


def _achat_numero_suivant(achat_at):
    return str(AchatCode.objects.filter(achat_at=achat_at).count() + 1)


class AchatNumeroSuivantView(GroupRequiredMixin, View):
    """Petit point d'entrée AJAX utilisé par le template Nouveau achat : à
    chaque changement du champ Date achat, recalcule le Numéro achat suivant
    pour cette date (nombre d'AchatCode existants ce jour-là + 1)."""
    group_required = 'Commercial'

    def get(self, request):
        achat_at_str = request.GET.get('achat_at', '').strip()
        try:
            achat_at = date.fromisoformat(achat_at_str)
        except ValueError:
            achat_at = date.today()
        return JsonResponse({'achat_numero': _achat_numero_suivant(achat_at)})


def _decimal_or_zero(value):
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal('0')


def _quantize(value):
    return value.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)


def _is_iso_date(value):
    try:
        date.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def _achat_create_context(request, **overrides):
    fournisseurs_list = (
        Fournisseur.objects
        .filter(type_fournisseur=FOURNISSEUR_TYPE_ACHAT)
        .order_by('raison_sociale')
    )
    matieres_list = MatierePremiere.objects.order_by('nom')
    today = date.today()

    context = {
        'achat_at':              today.isoformat(),
        'achat_numero':          _achat_numero_suivant(today),
        'fournisseur_id':        '',
        'nouveau_acompte':       '0',
        'mode_paiement':         'Espèces',
        'observation':           '',
        'fournisseurs_list':     fournisseurs_list,
        'mode_paiement_choices': MODE_PAIEMENT_CHOICES,
        'matieres_json':         json.dumps([
            {'id': m.pk, 'nom': m.nom, 'unite': m.unite or ''} for m in matieres_list
        ]),
        'unite_rows_json':       '[]',
        'pack_rows_json':        '[]',
        'focus_field':           None,
    }
    context.update(overrides)
    return context


class AchatCreateView(GroupRequiredMixin, View):
    """Formulaire « Nouveau achat » : Partie 1 (informations générales),
    Partie 2 (détails par unité), Partie 3 (détails par pack). Voir
    _achat_create_context() pour l'affichage et post() pour l'enregistrement
    (validations puis écriture atomique — AchatCode, Achat, AchatDetaille,
    Transaction caisse)."""
    group_required = 'Commercial'
    template_name = 'commercial/achat/create.html'

    def get(self, request):
        return render(request, self.template_name, _achat_create_context(request))

    def post(self, request):
        achat_at_str      = request.POST.get('achat_at', '').strip()
        fournisseur_id    = request.POST.get('fournisseur_id', '').strip()
        nouveau_acompte_s = request.POST.get('nouveau_acompte', '').strip()
        mode_paiement     = request.POST.get('mode_paiement', '').strip() or 'Espèces'
        observation       = request.POST.get('observation', '').strip()

        unite_mp_ids  = request.POST.getlist('detail_unite_matiere_premiere_id')
        unite_qtes    = request.POST.getlist('detail_unite_quantite')
        unite_prix    = request.POST.getlist('detail_unite_prix_unitaire')
        raw_unite_rows = [
            {'matiere_premiere_id': m, 'quantite': q, 'prix_unitaire': p}
            for m, q, p in zip(unite_mp_ids, unite_qtes, unite_prix)
        ]

        pack_mp_ids   = request.POST.getlist('detail_pack_matiere_premiere_id')
        pack_nombres  = request.POST.getlist('detail_pack_nombre_pack')
        pack_prix     = request.POST.getlist('detail_pack_prix_pack')
        pack_unites   = request.POST.getlist('detail_pack_unite_pack')
        raw_pack_rows = [
            {'matiere_premiere_id': m, 'nombre_pack': n, 'prix_pack': p, 'unite_pack': u}
            for m, n, p, u in zip(pack_mp_ids, pack_nombres, pack_prix, pack_unites)
        ]

        def _echec(message, *, focus_field=None):
            messages.error(request, message)
            context = _achat_create_context(
                request,
                achat_at=achat_at_str or date.today().isoformat(),
                achat_numero=_achat_numero_suivant(
                    date.fromisoformat(achat_at_str) if _is_iso_date(achat_at_str) else date.today()
                ),
                fournisseur_id=fournisseur_id,
                nouveau_acompte=nouveau_acompte_s or '0',
                mode_paiement=mode_paiement,
                observation=observation,
                unite_rows_json=json.dumps(raw_unite_rows),
                pack_rows_json=json.dumps(raw_pack_rows),
                focus_field=focus_field,
            )
            return render(request, self.template_name, context)

        # ── Étape 1 : validations des champs obligatoires (partie 1) ──────
        if not achat_at_str or not _is_iso_date(achat_at_str):
            return _echec("La date d'achat est obligatoire.")
        achat_at = date.fromisoformat(achat_at_str)

        if not fournisseur_id:
            return _echec('Le fournisseur est obligatoire.')

        if not nouveau_acompte_s:
            return _echec('Le montant payé est obligatoire.')
        nouveau_acompte = _decimal_or_zero(nouveau_acompte_s)
        if nouveau_acompte < 0:
            return _echec('Le montant payé doit être positif ou nul.', focus_field='nouveau_acompte')

        # ── Étape 2 : lignes valides (partie 2 : par unité) ────────────────
        # Ignorées silencieusement : matière première absente, quantité ou prix ≤ 0.
        unite_rows_valides = []
        for mp_id, qte_s, prix_s in zip(unite_mp_ids, unite_qtes, unite_prix):
            mp_id = (mp_id or '').strip()
            if not mp_id:
                continue
            qte = _decimal_or_zero(qte_s)
            prix = _decimal_or_zero(prix_s)
            if qte <= 0 or prix <= 0:
                continue
            unite_rows_valides.append({
                'matiere_premiere_id': mp_id,
                'quantite': _quantize(qte),
                'prix_unitaire': _quantize(prix),
                'total_ligne': _quantize(qte * prix),
            })

        # ── Étape 3 : lignes valides (partie 3 : par pack) ─────────────────
        # Ignorées silencieusement : matière première absente, nombre de pack,
        # prix pack ou unité par pack ≤ 0.
        pack_rows_valides = []
        for mp_id, nb_s, prixp_s, unitep_s in zip(pack_mp_ids, pack_nombres, pack_prix, pack_unites):
            mp_id = (mp_id or '').strip()
            if not mp_id:
                continue
            nombre_pack = _decimal_or_zero(nb_s)
            prix_pack = _decimal_or_zero(prixp_s)
            unite_pack = _decimal_or_zero(unitep_s)
            if nombre_pack <= 0 or prix_pack <= 0 or unite_pack <= 0:
                continue
            pack_rows_valides.append({
                'matiere_premiere_id': mp_id,
                'nombre_pack': _quantize(nombre_pack),
                'prix_pack': _quantize(prix_pack),
                'unite_pack': unite_pack,
                'quantite': _quantize(unite_pack * nombre_pack),
                'prix_unitaire': _quantize(prix_pack / unite_pack),
                'total_ligne': _quantize(nombre_pack * prix_pack),
            })

        if not unite_rows_valides and not pack_rows_valides:
            return _echec(
                "Aucune ligne de détail valide : ajoutez au moins un achat par unité ou par "
                "pack (matière première, quantités et prix strictement positifs)."
            )

        matieres_ids = {r['matiere_premiere_id'] for r in unite_rows_valides + pack_rows_valides}
        matieres_par_id = {
            str(mp.pk): mp for mp in MatierePremiere.objects.filter(pk__in=matieres_ids)
        }
        if len(matieres_par_id) != len(matieres_ids):
            return _echec(
                "Une ou plusieurs matières premières sélectionnées n'existent plus. "
                'Veuillez rafraîchir la page et recommencer.'
            )

        # ── Étape 4 : Montant total / Reste à payer ────────────────────────
        total_montant = sum((r['total_ligne'] for r in unite_rows_valides + pack_rows_valides), Decimal('0'))
        reste_a_payer = total_montant - nouveau_acompte
        if reste_a_payer < 0:
            return _echec(
                'Le montant total à payé ne peut pas être inférieur au montant payé',
                focus_field='nouveau_acompte',
            )

        if reste_a_payer == 0:
            statut = 'Payé'
        elif 0 < reste_a_payer < total_montant:
            statut = 'Partiellement payé'
        elif reste_a_payer == total_montant:
            statut = 'Non payé'
        else:
            statut = ''

        # Quantité achetée par matière première, tous détails valides confondus
        # (unité + pack) — sert à incrémenter MatierePremiere.quantite.
        quantite_achetee_par_mp = {}
        for r in unite_rows_valides + pack_rows_valides:
            mp_id = r['matiere_premiere_id']
            quantite_achetee_par_mp[mp_id] = quantite_achetee_par_mp.get(mp_id, Decimal('0')) + r['quantite']

        # ── Étape 5 : écriture atomique ─────────────────────────────────────
        try:
            with transaction.atomic():
                achat_code = AchatCode.objects.create(
                    achat_at=achat_at,
                    achat_numero=_achat_numero_suivant(achat_at),
                    fournisseur_id=fournisseur_id,
                )
                achat = Achat.objects.create(
                    achat_code=achat_code,
                    total_montant=total_montant,
                    total_acompte=nouveau_acompte,
                    nouveau_acompte=nouveau_acompte,
                    reste_a_payer=reste_a_payer,
                    mode_paiement=mode_paiement,
                    statut=statut,
                    observation=observation or None,
                )
                AchatDetaille.objects.bulk_create([
                    AchatDetaille(
                        achat=achat,
                        matiere_premiere=matieres_par_id[r['matiere_premiere_id']],
                        quantite=r['quantite'], prix_unitaire=r['prix_unitaire'],
                        total_ligne=r['total_ligne'], pack=False,
                    )
                    for r in unite_rows_valides
                ] + [
                    AchatDetaille(
                        achat=achat,
                        matiere_premiere=matieres_par_id[r['matiere_premiere_id']],
                        nombre_pack=r['nombre_pack'], prix_pack=r['prix_pack'],
                        unite_pack=int(r['unite_pack']),
                        quantite=r['quantite'], prix_unitaire=r['prix_unitaire'],
                        total_ligne=r['total_ligne'], pack=True,
                    )
                    for r in pack_rows_valides
                ])
                for mp_id, quantite_achetee in quantite_achetee_par_mp.items():
                    mp = matieres_par_id[mp_id]
                    mp.quantite = (mp.quantite or Decimal('0')) + quantite_achetee
                    mp.save(update_fields=['quantite'])

                libelle = LibelleTransaction.objects.get_or_create(libelle=LIBELLE_PAIEMENT_ACHAT)[0]
                Transaction.objects.create(
                    user=request.user,
                    created_at=date.today(),
                    libelle=libelle,
                    table_id=f'Achat_id_{achat.pk}',
                    montant=-nouveau_acompte,
                )
        except Exception:
            messages.error(
                request,
                "Échec de l'enregistrement de l'achat : aucune donnée n'a été modifiée.",
            )
            context = _achat_create_context(
                request,
                achat_at=achat_at_str, achat_numero=_achat_numero_suivant(achat_at),
                fournisseur_id=fournisseur_id, nouveau_acompte=nouveau_acompte_s or '0',
                mode_paiement=mode_paiement, observation=observation,
                unite_rows_json=json.dumps(raw_unite_rows), pack_rows_json=json.dumps(raw_pack_rows),
            )
            return render(request, self.template_name, context)

        log_audit(
            AuditAction.CREATE, f'Création achat {achat_code} — {total_montant} TND',
            table='Achat', record_id=achat.pk,
            new_value={'achat_code': model_to_dict(achat_code), 'achat': model_to_dict(achat)},
        )
        messages.success(request, f'Achat {achat_code} enregistré avec succès.')
        return redirect('commercial:achat-list')


class AchatDeleteView(GroupRequiredMixin, View):
    """Formulaire « Suppression d'une facture Achat de matières premières » —
    même structure d'affichage que AchatCreateView mais entièrement en
    lecture seule (aucun champ modifiable, pas d'ajout/suppression de ligne).

    post() : suppression effective, sous trois conditions (sinon la
    suppression est arrêtée sans rien modifier) :
      1. l'achat doit être le DERNIER enregistrement de la table Achat
         (le plus récent — pas de suppression « dans le désordre ») ;
      2. l'achat doit être l'UNIQUE Achat rattaché à son AchatCode ;
      3. la Transaction caisse liée (table_id="Achat_id_<achat_id>") ne doit
         pas déjà être intégrée à un calcul de solde (hist_solde_compte doit
         être NULL).
    Si les trois passent : suppression de la Transaction liée, décrément
    (plancher zéro) de MatierePremiere.quantite pour chaque ligne de détail,
    suppression des AchatDetaille puis de l'Achat, puis de l'AchatCode (la
    condition 2 garantit qu'aucun autre Achat n'en dépend encore)."""
    group_required = 'Commercial'
    template_name = 'commercial/achat/delete.html'
    PAGINATE_DETAILS = 4

    def get(self, request, pk):
        achat = get_object_or_404(
            Achat.objects.select_related('achat_code__fournisseur'), pk=pk,
        )
        fournisseurs_list = (
            Fournisseur.objects
            .filter(type_fournisseur=FOURNISSEUR_TYPE_ACHAT)
            .order_by('raison_sociale')
        )

        unite_qs = achat.details.filter(pack=False).select_related('matiere_premiere').order_by('pk')
        pack_qs  = achat.details.filter(pack=True).select_related('matiere_premiere').order_by('pk')

        unite_paginator = Paginator(unite_qs, self.PAGINATE_DETAILS)
        pack_paginator  = Paginator(pack_qs, self.PAGINATE_DETAILS)
        unite_page_obj = unite_paginator.get_page(request.GET.get('page_unite', 1))
        pack_page_obj  = pack_paginator.get_page(request.GET.get('page_pack', 1))

        return render(request, self.template_name, {
            'achat':                 achat,
            'achat_code':            achat.achat_code,
            'fournisseurs_list':     fournisseurs_list,
            'mode_paiement_choices': MODE_PAIEMENT_CHOICES,
            'statut_choices':        STATUT_PAIEMENT_CHOICES,
            'unite_page_obj':        unite_page_obj,
            'pack_page_obj':         pack_page_obj,
            'unite_is_paginated':    unite_page_obj.has_other_pages(),
            'pack_is_paginated':     pack_page_obj.has_other_pages(),
        })

    def post(self, request, pk):
        achat = get_object_or_404(Achat.objects.select_related('achat_code'), pk=pk)
        achat_code = achat.achat_code
        numero = str(achat_code)

        dernier_id = Achat.objects.order_by('-pk').values_list('pk', flat=True).first()
        if achat.pk != dernier_id:
            messages.error(
                request,
                f"Impossible de supprimer l'achat {numero} : seul le dernier achat enregistré "
                'peut être supprimé.',
            )
            return redirect('commercial:achat-list')

        if Achat.objects.filter(achat_code_id=achat_code.pk).exclude(pk=achat.pk).exists():
            messages.error(
                request,
                f"Impossible de supprimer l'achat {numero} : d'autres achats sont rattachés au "
                'même code achat.',
            )
            return redirect('commercial:achat-list')

        table_id = f'Achat_id_{achat.pk}'
        transaction_liee = Transaction.objects.filter(table_id=table_id).first()
        if transaction_liee is None:
            messages.error(
                request,
                f"Impossible de supprimer l'achat {numero} : la transaction de paiement "
                'associée est introuvable.',
            )
            return redirect('commercial:achat-list')
        if transaction_liee.hist_solde_compte_id is not None:
            messages.error(
                request,
                f"Impossible de supprimer l'achat {numero} : son paiement a déjà été intégré "
                "au calcul du solde d'un compte.",
            )
            return redirect('commercial:achat-list')

        details = list(achat.details.select_related('matiere_premiere'))
        nb_details = len(details)
        montant = achat.total_montant
        avant = {
            'achat': model_to_dict(achat),
            'achat_code': model_to_dict(achat_code),
            'details': [model_to_dict(d) for d in details],
            'transaction': model_to_dict(transaction_liee),
        }

        try:
            with transaction.atomic():
                transaction_liee.delete()

                quantites_par_mp = {}
                for d in details:
                    if d.matiere_premiere_id:
                        quantites_par_mp[d.matiere_premiere_id] = (
                            quantites_par_mp.get(d.matiere_premiere_id, Decimal('0')) + d.quantite
                        )
                matieres = {
                    mp.pk: mp for mp in MatierePremiere.objects.filter(pk__in=quantites_par_mp.keys())
                }
                for mp_id, quantite_achetee in quantites_par_mp.items():
                    mp = matieres[mp_id]
                    nouvelle_quantite = (mp.quantite or Decimal('0')) - quantite_achetee
                    if nouvelle_quantite < 0:
                        nouvelle_quantite = Decimal('0')
                    mp.quantite = nouvelle_quantite
                    mp.save(update_fields=['quantite'])

                achat.details.all().delete()
                achat.delete()
                achat_code.delete()
        except Exception:
            messages.error(
                request,
                f"Échec de la suppression de l'achat {numero} : aucune donnée n'a été modifiée.",
            )
            return redirect('commercial:achat-list')

        log_audit(
            AuditAction.DELETE,
            f'Suppression achat {numero} ({montant} TND, {nb_details} ligne(s) de détail)',
            table='Achat', record_id=pk, old_value=avant,
        )
        messages.success(
            request,
            f'Achat {numero} supprimé avec succès ({montant} TND, {nb_details} ligne(s) de détail).',
        )
        return redirect('commercial:achat-list')


def _achat_update_context(request, achat, **overrides):
    achat_code = achat.achat_code
    fournisseurs_list = (
        Fournisseur.objects
        .filter(type_fournisseur=FOURNISSEUR_TYPE_ACHAT)
        .order_by('raison_sociale')
    )
    matieres_list = MatierePremiere.objects.order_by('nom')

    unite_rows = [
        {
            'matiere_premiere_id': d.matiere_premiere_id,
            'quantite': str(d.quantite),
            'prix_unitaire': str(d.prix_unitaire),
        }
        for d in achat.details.filter(pack=False).order_by('pk')
    ]
    pack_rows = [
        {
            'matiere_premiere_id': d.matiere_premiere_id,
            'nombre_pack': str(d.nombre_pack),
            'prix_pack': str(d.prix_pack),
            'unite_pack': d.unite_pack,
        }
        for d in achat.details.filter(pack=True).order_by('pk')
    ]

    context = {
        'achat':                 achat,
        'achat_code':            achat_code,
        'achat_at':              achat_code.achat_at.isoformat(),
        'achat_numero':          achat_code.achat_numero,
        'fournisseur_id':        str(achat_code.fournisseur_id) if achat_code.fournisseur_id else '',
        # Montant payé repart de zéro : c'est un nouveau paiement, distinct du
        # total_acompte déjà réglé (voir champ Total acompte, verrouillé).
        'nouveau_acompte':       '0',
        'mode_paiement':         achat.mode_paiement,
        'observation':           achat.observation or '',
        'total_acompte':         str(achat.total_acompte),
        'fournisseurs_list':     fournisseurs_list,
        'mode_paiement_choices': MODE_PAIEMENT_CHOICES,
        'matieres_json':         json.dumps([
            {'id': m.pk, 'nom': m.nom, 'unite': m.unite or ''} for m in matieres_list
        ]),
        'unite_rows_json':       json.dumps(unite_rows),
        'pack_rows_json':        json.dumps(pack_rows),
        'focus_field':           None,
    }
    context.update(overrides)
    return context


class AchatUpdateView(GroupRequiredMixin, View):
    """Formulaire « Modification de l'enregistrement Achat des matières
    premières » — même structure que AchatCreateView (parties 2 et 3
    identiques : ajout/suppression de ligne et auto-calculs inchangés),
    sauf : Date achat verrouillée, Montant payé réinitialisé à 0, et un
    nouveau champ Total acompte (verrouillé, valeur figée = l'ancien
    Achat.total_acompte, jamais recalculé) inséré avant Reste à payer.

    post() : la table Achat est un historique — l'ancien enregistrement
    n'est jamais modifié ni supprimé. Seul AchatCode.fournisseur_id peut
    être mis à jour (achat_at/achat_numero restent figés). Un NOUVEL
    enregistrement Achat est créé (même achat_code_id), avec
    nouveau_acompte = Montant payé (saisi) et
    total_acompte = ancien total_acompte + Montant payé. Le reste de la
    procédure (validations, AchatDetaille, Transaction caisse) est identique
    à AchatCreateView, sauf le libellé de la Transaction.

    Stock (MatierePremiere.quantite) : avant d'appliquer l'incrément des
    nouvelles lignes de détail, la contribution de l'ANCIEN dernier Achat de
    ce même achat_code_id (ses AchatDetaille existants) est d'abord annulée
    (décrément, plancher zéro) — sinon les quantités achetées seraient
    comptées deux fois."""
    group_required = 'Commercial'
    template_name = 'commercial/achat/update.html'

    def get(self, request, pk):
        achat = get_object_or_404(
            Achat.objects.select_related('achat_code__fournisseur'), pk=pk,
        )
        return render(request, self.template_name, _achat_update_context(request, achat))

    def post(self, request, pk):
        achat = get_object_or_404(
            Achat.objects.select_related('achat_code__fournisseur'), pk=pk,
        )
        achat_code = achat.achat_code

        fournisseur_id    = request.POST.get('fournisseur_id', '').strip()
        nouveau_acompte_s = request.POST.get('nouveau_acompte', '').strip()
        mode_paiement     = request.POST.get('mode_paiement', '').strip() or 'Espèces'
        observation       = request.POST.get('observation', '').strip()

        unite_mp_ids  = request.POST.getlist('detail_unite_matiere_premiere_id')
        unite_qtes    = request.POST.getlist('detail_unite_quantite')
        unite_prix    = request.POST.getlist('detail_unite_prix_unitaire')
        raw_unite_rows = [
            {'matiere_premiere_id': m, 'quantite': q, 'prix_unitaire': p}
            for m, q, p in zip(unite_mp_ids, unite_qtes, unite_prix)
        ]

        pack_mp_ids   = request.POST.getlist('detail_pack_matiere_premiere_id')
        pack_nombres  = request.POST.getlist('detail_pack_nombre_pack')
        pack_prix     = request.POST.getlist('detail_pack_prix_pack')
        pack_unites   = request.POST.getlist('detail_pack_unite_pack')
        raw_pack_rows = [
            {'matiere_premiere_id': m, 'nombre_pack': n, 'prix_pack': p, 'unite_pack': u}
            for m, n, p, u in zip(pack_mp_ids, pack_nombres, pack_prix, pack_unites)
        ]

        def _echec(message, *, focus_field=None):
            messages.error(request, message)
            context = _achat_update_context(
                request, achat,
                fournisseur_id=fournisseur_id,
                nouveau_acompte=nouveau_acompte_s or '0',
                mode_paiement=mode_paiement,
                observation=observation,
                unite_rows_json=json.dumps(raw_unite_rows),
                pack_rows_json=json.dumps(raw_pack_rows),
                focus_field=focus_field,
            )
            return render(request, self.template_name, context)

        # ── Étape 1 : validations des champs obligatoires (partie 1) ──────
        if not fournisseur_id:
            return _echec('Le fournisseur est obligatoire.')

        if not nouveau_acompte_s:
            return _echec('Le montant payé est obligatoire.')
        nouveau_acompte = _decimal_or_zero(nouveau_acompte_s)
        if nouveau_acompte < 0:
            return _echec('Le montant payé doit être positif ou nul.', focus_field='nouveau_acompte')

        # ── Étape 2 : lignes valides (partie 2 : par unité) ────────────────
        unite_rows_valides = []
        for mp_id, qte_s, prix_s in zip(unite_mp_ids, unite_qtes, unite_prix):
            mp_id = (mp_id or '').strip()
            if not mp_id:
                continue
            qte = _decimal_or_zero(qte_s)
            prix = _decimal_or_zero(prix_s)
            if qte <= 0 or prix <= 0:
                continue
            unite_rows_valides.append({
                'matiere_premiere_id': mp_id,
                'quantite': _quantize(qte),
                'prix_unitaire': _quantize(prix),
                'total_ligne': _quantize(qte * prix),
            })

        # ── Étape 3 : lignes valides (partie 3 : par pack) ─────────────────
        pack_rows_valides = []
        for mp_id, nb_s, prixp_s, unitep_s in zip(pack_mp_ids, pack_nombres, pack_prix, pack_unites):
            mp_id = (mp_id or '').strip()
            if not mp_id:
                continue
            nombre_pack = _decimal_or_zero(nb_s)
            prix_pack = _decimal_or_zero(prixp_s)
            unite_pack = _decimal_or_zero(unitep_s)
            if nombre_pack <= 0 or prix_pack <= 0 or unite_pack <= 0:
                continue
            pack_rows_valides.append({
                'matiere_premiere_id': mp_id,
                'nombre_pack': _quantize(nombre_pack),
                'prix_pack': _quantize(prix_pack),
                'unite_pack': unite_pack,
                'quantite': _quantize(unite_pack * nombre_pack),
                'prix_unitaire': _quantize(prix_pack / unite_pack),
                'total_ligne': _quantize(nombre_pack * prix_pack),
            })

        if not unite_rows_valides and not pack_rows_valides:
            return _echec(
                "Aucune ligne de détail valide : ajoutez au moins un achat par unité ou par "
                "pack (matière première, quantités et prix strictement positifs)."
            )

        # Quantités des lignes de l'ANCIEN dernier Achat de ce même achat_code_id
        # (celui affiché/modifié ici) — utilisées pour annuler sa contribution au
        # stock avant d'appliquer celle des nouvelles lignes (voir étape 5).
        ancienne_quantite_par_mp = {}
        for d in achat.details.all():
            mp_id = str(d.matiere_premiere_id)
            ancienne_quantite_par_mp[mp_id] = ancienne_quantite_par_mp.get(mp_id, Decimal('0')) + d.quantite

        matieres_ids = {r['matiere_premiere_id'] for r in unite_rows_valides + pack_rows_valides}
        matieres_par_id = {
            str(mp.pk): mp
            for mp in MatierePremiere.objects.filter(pk__in=matieres_ids | set(ancienne_quantite_par_mp))
        }
        if not matieres_ids.issubset(matieres_par_id):
            return _echec(
                "Une ou plusieurs matières premières sélectionnées n'existent plus. "
                'Veuillez rafraîchir la page et recommencer.'
            )

        # ── Étape 4 : Montant total / Total acompte / Reste à payer ───────
        total_montant = sum((r['total_ligne'] for r in unite_rows_valides + pack_rows_valides), Decimal('0'))
        total_acompte = achat.total_acompte + nouveau_acompte
        reste_a_payer = total_montant - total_acompte
        if reste_a_payer < 0:
            return _echec(
                'Le montant total à payé ne peut pas être inférieur au montant payé',
                focus_field='nouveau_acompte',
            )

        if reste_a_payer == 0:
            statut = 'Payé'
        elif 0 < reste_a_payer < total_montant:
            statut = 'Partiellement payé'
        elif reste_a_payer == total_montant:
            statut = 'Non payé'
        else:
            statut = ''

        quantite_achetee_par_mp = {}
        for r in unite_rows_valides + pack_rows_valides:
            mp_id = r['matiere_premiere_id']
            quantite_achetee_par_mp[mp_id] = quantite_achetee_par_mp.get(mp_id, Decimal('0')) + r['quantite']

        # ── Étape 5 : écriture atomique ─────────────────────────────────────
        try:
            with transaction.atomic():
                if str(achat_code.fournisseur_id) != fournisseur_id:
                    achat_code.fournisseur_id = fournisseur_id
                    achat_code.save(update_fields=['fournisseur_id'])

                # Annule la contribution stock de l'ancien Achat (plancher zéro)
                # AVANT d'appliquer celle des nouvelles lignes ci-dessous.
                for mp_id, ancienne_quantite in ancienne_quantite_par_mp.items():
                    mp = matieres_par_id[mp_id]
                    nouvelle_quantite = (mp.quantite or Decimal('0')) - ancienne_quantite
                    if nouvelle_quantite < 0:
                        nouvelle_quantite = Decimal('0')
                    mp.quantite = nouvelle_quantite
                    mp.save(update_fields=['quantite'])

                nouvel_achat = Achat.objects.create(
                    achat_code=achat_code,
                    total_montant=total_montant,
                    total_acompte=total_acompte,
                    nouveau_acompte=nouveau_acompte,
                    reste_a_payer=reste_a_payer,
                    mode_paiement=mode_paiement,
                    statut=statut,
                    observation=observation or None,
                )
                AchatDetaille.objects.bulk_create([
                    AchatDetaille(
                        achat=nouvel_achat,
                        matiere_premiere=matieres_par_id[r['matiere_premiere_id']],
                        quantite=r['quantite'], prix_unitaire=r['prix_unitaire'],
                        total_ligne=r['total_ligne'], pack=False,
                    )
                    for r in unite_rows_valides
                ] + [
                    AchatDetaille(
                        achat=nouvel_achat,
                        matiere_premiere=matieres_par_id[r['matiere_premiere_id']],
                        nombre_pack=r['nombre_pack'], prix_pack=r['prix_pack'],
                        unite_pack=int(r['unite_pack']),
                        quantite=r['quantite'], prix_unitaire=r['prix_unitaire'],
                        total_ligne=r['total_ligne'], pack=True,
                    )
                    for r in pack_rows_valides
                ])
                for mp_id, quantite_achetee in quantite_achetee_par_mp.items():
                    mp = matieres_par_id[mp_id]
                    mp.quantite = (mp.quantite or Decimal('0')) + quantite_achetee
                    mp.save(update_fields=['quantite'])

                libelle = LibelleTransaction.objects.get_or_create(libelle=LIBELLE_PAIEMENT_ANCIEN_ACHAT)[0]
                Transaction.objects.create(
                    user=request.user,
                    created_at=date.today(),
                    libelle=libelle,
                    table_id=f'Achat_id_{nouvel_achat.pk}',
                    montant=-nouveau_acompte,
                )
        except Exception:
            messages.error(
                request,
                f"Échec de la modification de l'achat {achat_code} : aucune donnée n'a été modifiée.",
            )
            context = _achat_update_context(
                request, achat,
                fournisseur_id=fournisseur_id, nouveau_acompte=nouveau_acompte_s or '0',
                mode_paiement=mode_paiement, observation=observation,
                unite_rows_json=json.dumps(raw_unite_rows), pack_rows_json=json.dumps(raw_pack_rows),
            )
            return render(request, self.template_name, context)

        log_audit(
            AuditAction.UPDATE,
            f'Modification achat {achat_code} — nouvel enregistrement #{nouvel_achat.pk} ({total_montant} TND)',
            table='Achat', record_id=nouvel_achat.pk,
            old_value={'achat_code': model_to_dict(achat_code), 'achat': model_to_dict(achat)},
            new_value={'achat_code': model_to_dict(achat_code), 'achat': model_to_dict(nouvel_achat)},
        )
        messages.success(
            request,
            f'Achat {achat_code} modifié avec succès (nouvel enregistrement de {total_montant} TND).',
        )
        return redirect('commercial:achat-list')


# ─── Dépenses ───────────────────────────────────────────────────────────────

FOURNISSEUR_TYPE_DEPENSE_EXCLU = 'Matières premières et Emballages'


class DepenseListView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/depense/list.html'
    PAGINATE_BY = 6

    def get(self, request):
        today = date.today()
        is_admin = _is_administration(request.user)

        # ── Lecture des filtres GET ───────────────────────────────
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str   = request.GET.get('date_fin',   '').strip()
        fournisseur_id = request.GET.get('fournisseur_id', '').strip()
        statut         = request.GET.get('statut', '').strip()
        employe_id     = request.GET.get('employe_id', '').strip() if is_admin else ''

        # Valeurs par défaut : 7 derniers jours
        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        if date_debut > date_fin:
            messages.error(request, 'La date de début doit être antérieure ou égale à la date de fin.')
            date_debut = today - timedelta(days=6)
            date_fin = today
            date_debut_str = date_debut.isoformat()
            date_fin_str = date_fin.isoformat()

        # ── Queryset filtré ────────────────────────────────────────
        # La table Depense est un historique (même principe que Achat/BonLivraison) :
        # un même DepenseCode peut avoir plusieurs Depense. On affiche tous les
        # DepenseCode de la période, chacun représenté uniquement par son DERNIER
        # Depense (celui dont l'id est le plus grand pour ce depense_code_id).
        dernier_depense_id_par_code = (
            Depense.objects
            .filter(depense_code__depense_at__gte=date_debut, depense_code__depense_at__lte=date_fin)
            .values('depense_code_id')
            .annotate(dernier_id=Max('id'))
            .values_list('dernier_id', flat=True)
        )
        qs = Depense.objects.select_related('depense_code__fournisseur', 'user').filter(pk__in=dernier_depense_id_par_code)
        if fournisseur_id:
            qs = qs.filter(depense_code__fournisseur_id=fournisseur_id)
        if statut:
            qs = qs.filter(statut=statut)

        if not is_admin:
            qs = qs.filter(user=request.user)
        elif employe_id:
            qs = qs.filter(user_id=employe_id)

        qs = qs.order_by('depense_code__depense_at', 'pk')

        # ── Données pour les listes déroulantes ───────────────────
        fournisseurs_list = (
            Fournisseur.objects
            .exclude(type_fournisseur=FOURNISSEUR_TYPE_DEPENSE_EXCLU)
            .order_by('raison_sociale')
        )

        # ── Pagination ────────────────────────────────────────────
        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj  = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':          page_obj,
            'is_paginated':      page_obj.has_other_pages(),
            'date_debut':        date_debut_str,
            'date_fin':          date_fin_str,
            'fournisseur_id':    fournisseur_id,
            'statut':            statut,
            'employe_id':        employe_id,
            'is_administration': is_admin,
            'employes_list':     _depense_employes_list(),
            'fournisseurs_list': fournisseurs_list,
            'statut_choices':    STATUT_PAIEMENT_CHOICES,
            'total_count':       paginator.count,
        })


def _erreur_suppression_depense(is_admin_user, nb_depense_du_code, transaction_liee):
    """Retourne le message d'erreur bloquant la suppression de cette Depense,
    ou '' si toutes les conditions sont respectées."""
    if not is_admin_user:
        return "Vous n'êtes pas autorisé à supprimer cet enregistrement"
    if nb_depense_du_code > 1:
        return "l'enregistrement de la dépense est déjà modifié, il ne peut pas être supprimé"
    if transaction_liee is not None and transaction_liee.hist_solde_compte_id is not None:
        return 'La dépense est déjà enregistrée dans un solde caisse'
    return ''


class DepenseDeleteView(GroupRequiredMixin, View):
    """Suppression d'une dépense (voir _erreur_suppression_depense pour les
    conditions bloquantes). Ordre de suppression : Transaction liée, puis
    DepenseDetaille, puis Depense, puis DepenseCode — entièrement dans une
    transaction atomique."""
    group_required = ['Administration', 'Commercial']

    def post(self, request, pk):
        depense = get_object_or_404(Depense.objects.select_related('depense_code'), pk=pk)
        code = depense.depense_code
        numero = str(code)

        nb_depense_du_code = Depense.objects.filter(depense_code_id=code.pk).count()
        table_id = f'Depense_id_{depense.pk}'
        transaction_liee = Transaction.objects.filter(table_id=table_id).first()

        erreur = _erreur_suppression_depense(
            _is_administration(request.user), nb_depense_du_code, transaction_liee,
        )
        if erreur:
            messages.error(request, erreur)
            return redirect('commercial:depense-list')

        avant = {
            'depense_code': model_to_dict(code),
            'depense': model_to_dict(depense),
            'details': [model_to_dict(d) for d in depense.details.all()],
        }

        try:
            with transaction.atomic():
                if transaction_liee is not None:
                    transaction_liee.delete()
                depense.details.all().delete()
                depense.delete()
                code.delete()
        except Exception:
            messages.error(
                request,
                f"Échec de la suppression de la dépense {numero} : aucune donnée n'a été modifiée.",
            )
            return redirect('commercial:depense-list')

        log_audit(
            AuditAction.DELETE, f'Suppression dépense {numero}',
            table='DepenseCode', record_id=code.pk, old_value=avant,
        )
        messages.success(request, f'Dépense {numero} supprimée avec succès.')
        return redirect('commercial:depense-list')


def _depense_numero_suivant(depense_at):
    return str(DepenseCode.objects.filter(depense_at=depense_at).count() + 1)


class DepenseNumeroSuivantView(GroupRequiredMixin, View):
    """Petit point d'entrée AJAX utilisé par le template Nouvelle dépense : à
    chaque changement du champ Date dépense, recalcule le Numéro dépense
    suivant pour cette date (nombre de DepenseCode existants ce jour-là + 1)."""
    group_required = ['Administration', 'Commercial']

    def get(self, request):
        depense_at_str = request.GET.get('depense_at', '').strip()
        try:
            depense_at = date.fromisoformat(depense_at_str)
        except ValueError:
            depense_at = date.today()
        return JsonResponse({'depense_numero': _depense_numero_suivant(depense_at)})


LIBELLE_PAIEMENT_DEPENSE = 'Paiement nouvelle dépense'


def _quantize0(value):
    return value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def _depense_employes_list():
    return (
        User.objects.filter(is_active=True)
        .exclude(is_superuser=True)
        .order_by('last_name', 'first_name')
    )


def _depense_create_context(request, **overrides):
    today = date.today()
    is_admin = _is_administration(request.user)

    context = {
        'is_administration':     is_admin,
        'employe_id':            str(request.user.pk),
        'employes_list':         _depense_employes_list(),
        'depense_at':            today.isoformat(),
        'depense_numero':        _depense_numero_suivant(today),
        'fournisseur_id':        '',
        'nouveau_acompte':       '0',
        'mode_paiement':         'Espèces',
        'observation':           '',
        'fournisseurs_list': (
            Fournisseur.objects
            .exclude(type_fournisseur=FOURNISSEUR_TYPE_DEPENSE_EXCLU)
            .order_by('raison_sociale')
        ),
        'mode_paiement_choices': MODE_PAIEMENT_CHOICES,
        'detail_rows_json':      '[]',
    }
    context.update(overrides)
    return context


class DepenseCreateView(GroupRequiredMixin, View):
    """Formulaire « Nouvelle dépense » : Partie 1 (informations générales),
    Partie 2 (détails de la dépense). Voir _depense_create_context() pour
    l'affichage et post() pour l'enregistrement (validations puis écriture
    atomique — DepenseCode, Depense, DepenseDetaille, Transaction caisse)."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/depense/create.html'

    def get(self, request):
        return render(request, self.template_name, _depense_create_context(request))

    def post(self, request):
        employe_id        = request.POST.get('employe_id', '').strip()
        depense_at_str     = request.POST.get('depense_at', '').strip()
        fournisseur_id     = request.POST.get('fournisseur_id', '').strip()
        nouveau_acompte_s  = request.POST.get('nouveau_acompte', '').strip()
        mode_paiement      = request.POST.get('mode_paiement', '').strip() or 'Espèces'
        observation        = request.POST.get('observation', '').strip()

        libelles  = request.POST.getlist('detail_libelle')
        quantites = request.POST.getlist('detail_quantite')
        valeurs   = request.POST.getlist('detail_valeur_unitaire')
        raw_detail_rows = [
            {'libelle': l, 'quantite': q, 'valeur_unitaire': v}
            for l, q, v in zip(libelles, quantites, valeurs)
        ]

        def _echec(message):
            messages.error(request, message)
            context = _depense_create_context(
                request,
                employe_id=employe_id or str(request.user.pk),
                depense_at=depense_at_str or date.today().isoformat(),
                depense_numero=_depense_numero_suivant(
                    date.fromisoformat(depense_at_str) if _is_iso_date(depense_at_str) else date.today()
                ),
                fournisseur_id=fournisseur_id,
                nouveau_acompte=nouveau_acompte_s or '0',
                mode_paiement=mode_paiement,
                observation=observation,
                detail_rows_json=json.dumps(raw_detail_rows),
            )
            return render(request, self.template_name, context)

        # ── Étape 1 : validations des champs de la partie 1 ────────────────
        if not employe_id:
            return _echec("L'employé doit être défini")

        if not depense_at_str or not _is_iso_date(depense_at_str):
            return _echec('La date dépense doit être définie')
        depense_at = date.fromisoformat(depense_at_str)

        if not fournisseur_id:
            return _echec('Le fournisseur doit être défini')

        nouveau_acompte = _decimal_or_zero(nouveau_acompte_s)

        # ── Étape 2 : lignes valides de la partie 2 ─────────────────────────
        # Ignorées silencieusement : libellé vide, quantité ou valeur unitaire ≤ 0.
        detail_rows_valides = []
        for libelle, qte_s, val_s in zip(libelles, quantites, valeurs):
            libelle = (libelle or '').strip()
            if not libelle:
                continue
            qte = _decimal_or_zero(qte_s)
            val = _decimal_or_zero(val_s)
            if qte <= 0 or val <= 0:
                continue
            detail_rows_valides.append({
                'libelle': libelle,
                'quantite': _quantize0(qte),
                'valeur_unitaire': _quantize(val),
                'total_ligne': _quantize(qte * val),
            })

        # ── Étape 3 : Montant total / Reste à payer ─────────────────────────
        total_montant = sum((r['total_ligne'] for r in detail_rows_valides), Decimal('0'))
        if total_montant <= 0:
            return _echec('Le montant total doit être suppérieur à zéro')

        reste_a_payer = total_montant - nouveau_acompte
        if reste_a_payer < 0:
            return _echec('Le reste ne doit pas être nigatif')

        if not detail_rows_valides:
            return _echec('la section Détails de la dépense doit contenir au moins un enregistrement valide')

        if reste_a_payer == 0:
            statut = 'Payé'
        elif 0 < reste_a_payer < total_montant:
            statut = 'Partiellement payé'
        elif reste_a_payer == total_montant:
            statut = 'Non payé'
        else:
            statut = ''

        depense_numero = _depense_numero_suivant(depense_at)

        # ── Étape 4 : écriture atomique ──────────────────────────────────────
        try:
            with transaction.atomic():
                depense_code = DepenseCode.objects.create(
                    depense_at=depense_at,
                    depense_numero=depense_numero,
                    fournisseur_id=fournisseur_id,
                )
                depense = Depense.objects.create(
                    depense_code=depense_code,
                    user_id=employe_id,
                    nouveau_acompte=nouveau_acompte,
                    total_acompte=nouveau_acompte,
                    mode_paiement=mode_paiement,
                    observation=observation or None,
                )
                DepenseDetaille.objects.bulk_create([
                    DepenseDetaille(
                        depense=depense,
                        libelle=r['libelle'],
                        quantite=r['quantite'],
                        valeur_unitaire=r['valeur_unitaire'],
                        total_ligne=r['total_ligne'],
                    )
                    for r in detail_rows_valides
                ])

                depense.total_montant = total_montant
                depense.reste_a_payer = reste_a_payer
                depense.statut = statut
                depense.save(update_fields=['total_montant', 'reste_a_payer', 'statut'])

                libelle_obj = LibelleTransaction.objects.get_or_create(libelle=LIBELLE_PAIEMENT_DEPENSE)[0]
                Transaction.objects.create(
                    user=depense.user,
                    created_at=date.today(),
                    libelle=libelle_obj,
                    table_id=f'Depense_id_{depense.pk}',
                    montant=-nouveau_acompte,
                    hist_solde_compte=None,
                )
        except Exception:
            messages.error(
                request,
                "Échec de l'enregistrement de la dépense : aucune donnée n'a été modifiée.",
            )
            context = _depense_create_context(
                request,
                employe_id=employe_id, depense_at=depense_at_str, depense_numero=depense_numero,
                fournisseur_id=fournisseur_id, nouveau_acompte=nouveau_acompte_s or '0',
                mode_paiement=mode_paiement, observation=observation,
                detail_rows_json=json.dumps(raw_detail_rows),
            )
            return render(request, self.template_name, context)

        log_audit(
            AuditAction.CREATE, f'Création dépense {depense_code} — {total_montant} TND',
            table='Depense', record_id=depense.pk,
            new_value={'depense_code': model_to_dict(depense_code), 'depense': model_to_dict(depense)},
        )
        messages.success(request, f'Dépense {depense_code} enregistrée avec succès.')
        return redirect('commercial:depense-list')


LIBELLE_PAIEMENT_DEPENSE_MODIFIEE = 'Paiement dépense modifiée'


class DepenseUpdateView(GroupRequiredMixin, View):
    """Formulaire « Modification de la dépense » : Section 1 (informations
    générales), Section 2 (détails, entièrement éditable — libellé, quantité,
    valeur unitaire). L'enregistrement (voir post()) crée un nouvel
    enregistrement historique dans commercial_depense plutôt que de modifier
    l'existant — même principe que BonLivraisonUpdateView."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/depense/update.html'

    def _get_depense(self, request, pk):
        depense = get_object_or_404(
            Depense.objects.select_related('depense_code__fournisseur', 'user'), pk=pk,
        )
        is_admin = _is_administration(request.user)
        return depense, is_admin

    def _est_autorise(self, request, depense, is_admin):
        return is_admin or depense.user_id == request.user.pk

    def _context(self, request, depense, is_admin, **overrides):
        code = depense.depense_code
        details = depense.details.order_by('pk')

        detail_rows_json = overrides.pop('detail_rows_json', None)
        if detail_rows_json is None:
            detail_rows_json = json.dumps([
                {
                    'libelle': d.libelle or '',
                    'quantite': str(d.quantite),
                    'valeur_unitaire': str(d.valeur_unitaire),
                }
                for d in details
            ])

        context = {
            'depense':               depense,
            'depense_code':          code,
            'is_administration':     is_admin,
            'employe_id':            str(depense.user_id) if is_admin else str(request.user.pk),
            'employes_list':         _depense_employes_list(),
            'depense_at':            code.depense_at.isoformat(),
            'depense_numero':        code.depense_numero,
            'fournisseur_id':        str(code.fournisseur_id),
            'ancien_total_acompte':  str(depense.total_acompte),
            'nouveau_acompte':       '0',
            'mode_paiement':         'Espèces',
            'observation':           '',
            'fournisseurs_list': (
                Fournisseur.objects
                .exclude(type_fournisseur=FOURNISSEUR_TYPE_DEPENSE_EXCLU)
                .order_by('raison_sociale')
            ),
            'mode_paiement_choices': MODE_PAIEMENT_CHOICES,
            'detail_rows_json':      detail_rows_json,
        }
        context.update(overrides)
        return context

    def get(self, request, pk):
        depense, is_admin = self._get_depense(request, pk)
        if not self._est_autorise(request, depense, is_admin):
            messages.error(request, "Vous n'êtes pas autorisé à modifier l'enregistrement")
            return redirect('commercial:depense-list')
        return render(request, self.template_name, self._context(request, depense, is_admin))

    def post(self, request, pk):
        depense, is_admin = self._get_depense(request, pk)
        if not self._est_autorise(request, depense, is_admin):
            messages.error(request, "Vous n'êtes pas autorisé à modifier l'enregistrement")
            return redirect('commercial:depense-list')

        code = depense.depense_code

        employe_id        = request.POST.get('employe_id', '').strip() if is_admin else str(request.user.pk)
        fournisseur_id     = request.POST.get('fournisseur_id', '').strip()
        nouveau_acompte_s  = request.POST.get('nouveau_acompte', '').strip()
        mode_paiement      = request.POST.get('mode_paiement', '').strip() or 'Espèces'
        observation        = request.POST.get('observation', '').strip()

        libelles  = request.POST.getlist('detail_libelle')
        quantites = request.POST.getlist('detail_quantite')
        valeurs   = request.POST.getlist('detail_valeur_unitaire')
        raw_detail_rows = [
            {'libelle': l, 'quantite': q, 'valeur_unitaire': v}
            for l, q, v in zip(libelles, quantites, valeurs)
        ]

        def _echec(message):
            messages.error(request, message)
            context = self._context(
                request, depense, is_admin,
                employe_id=employe_id or str(depense.user_id if is_admin else request.user.pk),
                fournisseur_id=fournisseur_id,
                nouveau_acompte=nouveau_acompte_s or '0',
                mode_paiement=mode_paiement,
                observation=observation,
                detail_rows_json=json.dumps(raw_detail_rows),
            )
            return render(request, self.template_name, context)

        # ── Étape 1 : validations ───────────────────────────────────────────
        if not fournisseur_id:
            return _echec('Le fournisseur doit être défini')

        nouveau_acompte = _decimal_or_zero(nouveau_acompte_s)

        # ── Étape 2 : lignes valides de la section 2 ────────────────────────
        # Ignorées silencieusement : libellé vide, quantité ou valeur unitaire ≤ 0.
        detail_rows_valides = []
        for libelle, qte_s, val_s in zip(libelles, quantites, valeurs):
            libelle = (libelle or '').strip()
            if not libelle:
                continue
            qte = _decimal_or_zero(qte_s)
            val = _decimal_or_zero(val_s)
            if qte <= 0 or val <= 0:
                continue
            detail_rows_valides.append({
                'libelle': libelle,
                'quantite': _quantize0(qte),
                'valeur_unitaire': _quantize(val),
                'total_ligne': _quantize(qte * val),
            })

        # ── Étape 3 : Montant total / Total acompte / Reste à payer ─────────
        total_montant = sum((r['total_ligne'] for r in detail_rows_valides), Decimal('0'))
        if total_montant <= 0:
            return _echec('Le montant total doit être suppérieur à zéro')

        ancien_total_acompte = depense.total_acompte
        total_acompte = ancien_total_acompte + nouveau_acompte
        reste_a_payer = total_montant - total_acompte
        if reste_a_payer < 0:
            return _echec('Le reste ne doit pas être nigatif')

        if not detail_rows_valides:
            return _echec('la section Détails de la dépense doit contenir au moins un enregistrement valide')

        if reste_a_payer == 0:
            statut = 'Payé'
        elif 0 < reste_a_payer < total_montant:
            statut = 'Partiellement payé'
        elif reste_a_payer == total_montant:
            statut = 'Non payé'
        else:
            statut = ''

        # ── Étape 4 : écriture atomique ──────────────────────────────────────
        try:
            with transaction.atomic():
                if code.fournisseur_id != int(fournisseur_id):
                    code.fournisseur_id = fournisseur_id
                    code.save(update_fields=['fournisseur'])

                nouvelle_depense = Depense.objects.create(
                    depense_code=code,
                    user_id=employe_id,
                    nouveau_acompte=nouveau_acompte,
                    total_acompte=total_acompte,
                    mode_paiement=mode_paiement,
                    observation=observation or None,
                )
                DepenseDetaille.objects.bulk_create([
                    DepenseDetaille(
                        depense=nouvelle_depense,
                        libelle=r['libelle'],
                        quantite=r['quantite'],
                        valeur_unitaire=r['valeur_unitaire'],
                        total_ligne=r['total_ligne'],
                    )
                    for r in detail_rows_valides
                ])

                nouvelle_depense.total_montant = total_montant
                nouvelle_depense.reste_a_payer = reste_a_payer
                nouvelle_depense.statut = statut
                nouvelle_depense.save(update_fields=['total_montant', 'reste_a_payer', 'statut'])

                libelle_obj = LibelleTransaction.objects.get_or_create(
                    libelle=LIBELLE_PAIEMENT_DEPENSE_MODIFIEE,
                )[0]
                Transaction.objects.create(
                    user=nouvelle_depense.user,
                    created_at=date.today(),
                    libelle=libelle_obj,
                    table_id=f'Depense_id_{nouvelle_depense.pk}',
                    montant=-nouveau_acompte,
                    hist_solde_compte=None,
                )
        except Exception:
            messages.error(
                request,
                "Échec de la modification de la dépense : aucune donnée n'a été modifiée.",
            )
            context = self._context(
                request, depense, is_admin,
                employe_id=employe_id, fournisseur_id=fournisseur_id,
                nouveau_acompte=nouveau_acompte_s or '0',
                mode_paiement=mode_paiement, observation=observation,
                detail_rows_json=json.dumps(raw_detail_rows),
            )
            return render(request, self.template_name, context)

        log_audit(
            AuditAction.UPDATE, f'Modification dépense {code} — {total_montant} TND',
            table='Depense', record_id=nouvelle_depense.pk,
            old_value=model_to_dict(depense), new_value=model_to_dict(nouvelle_depense),
        )
        messages.success(request, f'Dépense {code} modifiée avec succès.')
        return redirect('commercial:depense-list')


# ─── Agenda ─────────────────────────────────────────────────────────────────

def _agenda_commerciaux_list():
    return User.objects.filter(groups__name='Commercial').order_by('last_name', 'first_name')


class AgendaListView(GroupRequiredMixin, View):
    """Page « Suivi des actions commerciales planifiées » : Section 1
    (filtres), Section 2 (liste modifiable/supprimable ligne par ligne, voir
    AgendaUpdateView/AgendaDeleteView), Section 3 (ajout multi-lignes, voir
    AgendaCreateView)."""
    group_required = ['Administration', 'Commercial']
    template_name = 'commercial/agenda/list.html'
    PAGINATE_BY = 6

    def get(self, request):
        today = date.today()
        is_admin = _is_administration(request.user)

        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str    = request.GET.get('date_fin', '').strip()
        client_id       = request.GET.get('client_id', '').strip()
        statut          = request.GET.get('statut', '').strip()
        commercial_id   = request.GET.get('commercial_id', '').strip() if is_admin else str(request.user.pk)

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        if date_debut > date_fin:
            messages.error(request, 'La date de début doit être antérieure ou égale à la date de fin.')
            date_debut = today - timedelta(days=6)
            date_fin = today
            date_debut_str = date_debut.isoformat()
            date_fin_str = date_fin.isoformat()

        qs = Agenda.objects.select_related('user', 'client').filter(
            echeance_at__gte=date_debut, echeance_at__lte=date_fin,
        )
        if not is_admin:
            qs = qs.filter(user=request.user)
        elif commercial_id:
            qs = qs.filter(user_id=commercial_id)
        if client_id:
            qs = qs.filter(client_id=client_id)
        if statut:
            qs = qs.filter(status=statut)
        qs = qs.order_by('echeance_at')

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        if not is_admin:
            clients_filtre_list = _client_queryset_for_user(request.user).order_by('raison_sociale')
        elif commercial_id:
            clients_filtre_list = Client.objects.filter(
                client_users__user_id=commercial_id,
            ).order_by('raison_sociale').distinct()
        else:
            clients_filtre_list = Client.objects.order_by('raison_sociale')

        # ── Section 3 : listes déroulantes pour l'ajout de nouvelles lignes ──
        # (Commercial → Zone → Client en cascade, même principe que
        # BonLivraisonCreateView : row_zones_json/row_clients_json contiennent
        # systématiquement TOUT — le filtrage par commercial/zone est fait
        # côté JS via client_users_json et le zone_id de chaque client.)
        if is_admin:
            row_commerciaux_list = _agenda_commerciaux_list()
            row_clients_list = Client.objects.select_related('zone').order_by('raison_sociale')
        else:
            row_commerciaux_list = []
            row_clients_list = _client_queryset_for_user(request.user).select_related('zone').order_by('raison_sociale')

        row_zones_list = Zone.objects.filter(
            clients__in=row_clients_list,
        ).distinct().order_by('nom')

        return render(request, self.template_name, {
            'agendas':               page_obj.object_list,
            'page_obj':              page_obj,
            'is_paginated':          page_obj.has_other_pages(),
            'total_count':           paginator.count,
            'is_administration':     is_admin,
            'commercial_id':         commercial_id,
            'date_debut':            date_debut_str,
            'date_fin':              date_fin_str,
            'client_id':             client_id,
            'statut':                statut,
            'commerciaux_list':      _agenda_commerciaux_list() if is_admin else None,
            'clients_filtre_list':   clients_filtre_list,
            'statut_choices':        Agenda.STATUS_CHOICES,
            'today_iso':             today.isoformat(),
            'row_commerciaux_json': json.dumps([
                {'id': u.pk, 'label': u.get_full_name() or u.username} for u in row_commerciaux_list
            ]),
            'row_clients_json': json.dumps([
                {'id': c.pk, 'label': str(c), 'zone_id': c.zone_id} for c in row_clients_list
            ]),
            'row_zones_json': json.dumps([
                {'id': z.pk, 'nom': z.nom} for z in row_zones_list
            ]),
            'client_users_json': json.dumps([
                {'client_id': cu.client_id, 'user_id': cu.user_id}
                for cu in (ClientUser.objects.all() if is_admin else ClientUser.objects.filter(user=request.user))
            ]),
        })


class AgendaUpdateView(GroupRequiredMixin, View):
    """Mise à jour directe (pas d'historique) d'une ligne Agenda depuis la
    Section 2 : seuls detaille_action_planifier / echeance_at / status sont
    modifiables (Commercial et Client restent verrouillés)."""
    group_required = ['Administration', 'Commercial']

    def post(self, request, pk):
        qs = Agenda.objects.all()
        if not _is_administration(request.user):
            qs = qs.filter(user=request.user)
        agenda = get_object_or_404(qs, pk=pk)
        avant = model_to_dict(agenda)

        detaille = request.POST.get('detaille_action_planifier', '').strip()
        echeance_str = request.POST.get('echeance_at', '').strip()
        statut = request.POST.get('status', '').strip()

        agenda.detaille_action_planifier = detaille or None
        if _is_iso_date(echeance_str):
            agenda.echeance_at = date.fromisoformat(echeance_str)
        agenda.status = statut or agenda.status

        try:
            agenda.save(update_fields=['detaille_action_planifier', 'echeance_at', 'status'])
        except Exception:
            messages.error(request, "Échec de la modification de l'action planifiée.")
            return redirect('commercial:agenda-list')

        log_audit(
            AuditAction.UPDATE, "Modification d'une action planifiée (Agenda)",
            table='Agenda', record_id=agenda.pk, old_value=avant, new_value=model_to_dict(agenda),
        )
        messages.success(request, 'Action planifiée modifiée avec succès.')
        return redirect('commercial:agenda-list')


class AgendaDeleteView(GroupRequiredMixin, View):
    """Suppression directe d'une ligne Agenda depuis la Section 2."""
    group_required = ['Administration', 'Commercial']

    def post(self, request, pk):
        qs = Agenda.objects.all()
        if not _is_administration(request.user):
            qs = qs.filter(user=request.user)
        agenda = get_object_or_404(qs, pk=pk)
        avant = model_to_dict(agenda)

        try:
            agenda.delete()
        except Exception:
            messages.error(request, "Échec de la suppression de l'action planifiée.")
            return redirect('commercial:agenda-list')

        log_audit(
            AuditAction.DELETE, "Suppression d'une action planifiée (Agenda)",
            table='Agenda', record_id=pk, old_value=avant,
        )
        messages.success(request, 'Action planifiée supprimée avec succès.')
        return redirect('commercial:agenda-list')


class AgendaCreateView(GroupRequiredMixin, View):
    """Ajout multi-lignes depuis la Section 3 : chaque ligne est validée
    indépendamment (Commercial, Client, Action planifiée, Échéance tous non
    vides) — les lignes invalides sont ignorées en silence."""
    group_required = ['Administration', 'Commercial']

    def post(self, request):
        is_admin = _is_administration(request.user)

        commercial_ids = request.POST.getlist('row_commercial_id')
        client_ids     = request.POST.getlist('row_client_id')
        details        = request.POST.getlist('row_detaille_action_planifier')
        echeances      = request.POST.getlist('row_echeance_at')
        statuts        = request.POST.getlist('row_status')

        nb_total = len(details)
        lignes_valides = []
        for com_id, cli_id, detail, ech_str, statut in zip(commercial_ids, client_ids, details, echeances, statuts):
            com_id = com_id.strip() if is_admin else str(request.user.pk)
            cli_id = (cli_id or '').strip()
            detail = (detail or '').strip()
            ech_str = (ech_str or '').strip()
            statut = (statut or '').strip() or 'En attente'

            if not com_id or not cli_id or not detail or not ech_str or not _is_iso_date(ech_str):
                continue

            lignes_valides.append({
                'user_id': com_id,
                'client_id': cli_id,
                'detaille_action_planifier': detail,
                'echeance_at': date.fromisoformat(ech_str),
                'status': statut,
            })

        nb_valides = len(lignes_valides)
        nb_invalides = nb_total - nb_valides

        if nb_valides == 0:
            messages.error(
                request,
                "Aucun enregistrement valide n'a été ajouté : vérifiez que le Commercial, le Client, "
                "l'Action planifiée et l'Échéance sont bien renseignés pour au moins une ligne.",
            )
            return redirect('commercial:agenda-list')

        try:
            with transaction.atomic():
                crees = [Agenda.objects.create(**l) for l in lignes_valides]
        except Exception:
            messages.error(
                request,
                "Échec de l'ajout des actions planifiées : aucune donnée n'a été modifiée.",
            )
            return redirect('commercial:agenda-list')

        log_audit(
            AuditAction.CREATE, f'Ajout de {nb_valides} action(s) planifiée(s) (Agenda)',
            table='Agenda', new_value=[model_to_dict(a) for a in crees],
        )
        if nb_invalides:
            messages.success(
                request,
                f'{nb_valides} enregistrement(s) ajouté(s) avec succès. '
                f'{nb_invalides} ligne(s) ignorée(s) (champs manquants).',
            )
        else:
            messages.success(request, f'{nb_valides} enregistrement(s) ajouté(s) avec succès.')
        return redirect('commercial:agenda-list')
