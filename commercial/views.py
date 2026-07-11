import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.forms.models import model_to_dict
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import TemplateView, View

from administration.models import LibelleTransaction, Transaction
from core.audit import AuditAction, log_audit
from core.mixins import GroupRequiredMixin
from production.models import MatierePremiere

from .models import Achat, AchatCode, AchatDetaille, Fournisseur, MODE_PAIEMENT_CHOICES, STATUT_PAIEMENT_CHOICES

FOURNISSEUR_TYPE_ACHAT = 'Matières premières et Emballages'
LIBELLE_PAIEMENT_ACHAT = 'Paiement nouveau achat matière première'


class HomeView(GroupRequiredMixin, TemplateView):
    group_required = 'Commercial'
    template_name = 'commercial/home.html'


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
        qs = Achat.objects.select_related('achat_code__fournisseur').filter(
            achat_code__achat_at__gte=date_debut,
            achat_code__achat_at__lte=date_fin,
        )
        if fournisseur_id:
            qs = qs.filter(achat_code__fournisseur_id=fournisseur_id)
        if statut:
            qs = qs.filter(statut=statut)

        qs = qs.order_by('achat_code__achat_at', 'achat_code__achat_numero')

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
                    montant=nouveau_acompte,
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
