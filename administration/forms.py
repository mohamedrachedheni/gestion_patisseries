from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row

from commercial.models import AbsenceCommercial, Delegation, Gouvernorat, Zone
from core.models import JourNonOuvre

from .models import Employe, Transfere

User = get_user_model()

TYPE_CONTRAT_CHOICES = [
    ('', '— Choisir —'),
    ('CDI', 'CDI'),
    ('CDD', 'CDD'),
    ('Intérim', 'Intérim'),
    ('Stage', 'Stage'),
    ('Freelance', 'Freelance'),
]


_AUTOFILL_BLOCK_ATTRS = {
    # Chrome peut ignorer « off » sur les champs qu'il reconnaît comme une
    # adresse ou un identifiant. « new-password » empêche cette classification
    # sans changer le type, le nom ou la valeur envoyée au serveur.
    'autocomplete': 'new-password',
    'aria-autocomplete': 'none',
    # Les principaux gestionnaires de mots de passe disposent chacun de leur
    # attribut d'exclusion. Ils sont sans effet sur Django et les navigateurs
    # qui ne les connaissent pas.
    'data-lpignore': 'true',
    'data-1p-ignore': 'true',
    'data-bwignore': 'true',
    'data-form-type': 'other',
}


def _block_browser_autofill(form, *, lock_text_fields=False):
    """Désactive l'autofill natif et celui des extensions sur un formulaire."""
    ignored_widgets = (forms.HiddenInput, forms.CheckboxInput, forms.FileInput, forms.Select)

    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, ignored_widgets):
            continue
        widget.attrs.update(_AUTOFILL_BLOCK_ATTRS)
        if lock_text_fields and (
            isinstance(widget, forms.Textarea) or getattr(widget, 'input_type', None)
        ):
            # Lecture seule au chargement : empêche l'injection automatique.
            # Le template retire cet attribut dès un vrai focus utilisateur.
            widget.attrs['readonly'] = True
            widget.attrs['data-autofill-lock'] = 'true'


class UserCreateForm(UserCreationForm):
    is_staff = forms.BooleanField(
        required=False,
        label='Accès interface Django admin',
        help_text='Disponible uniquement pour le groupe Administration.',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input', 'disabled': True}),
    )
    groupe = forms.ModelChoiceField(
        queryset=Group.objects.all(),
        required=False,
        empty_label='— Aucun groupe —',
        label='Groupe de permissions',
    )

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'username', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        self.fields['email'].required = True
        # UserCreationForm met « autofocus » en dur sur username — on le
        # retire pour respecter l'ordre visuel du formulaire (Prénom en premier).
        del self.fields['username'].widget.attrs['autofocus']
        self.fields['first_name'].widget.attrs['autofocus'] = True
        _block_browser_autofill(self)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column('first_name', css_class='col-md-6'),
                Column('last_name',  css_class='col-md-6'),
            ),
            Row(
                Column('username', css_class='col-md-6'),
                Column('email',    css_class='col-md-6'),
            ),
            Row(
                Column('password1', css_class='col-md-6'),
                Column('password2', css_class='col-md-6'),
            ),
            Row(
                Column('groupe',   css_class='col-md-8'),
                Column('is_staff', css_class='col-md-4 d-flex align-items-center pt-2'),
            ),
        )

    def save(self, commit=True):
        user = super().save(commit=False)
        groupe = self.cleaned_data.get('groupe')
        # L'accès à l'interface Django admin n'est autorisé que pour le groupe Administration,
        # quelle que soit la case cochée côté client (la case est verrouillée par JS sinon).
        user.is_staff = bool(self.cleaned_data.get('is_staff', False)) and bool(groupe) and groupe.name == 'Administration'
        if commit:
            user.save()
            user.groups.set([groupe] if groupe else [])
        return user


class UserUpdateForm(forms.ModelForm):
    is_staff = forms.BooleanField(
        required=False,
        label='Accès interface Django admin',
        help_text='Disponible uniquement pour le groupe Administration.',
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input', 'disabled': True}),
    )
    groupe = forms.ModelChoiceField(
        queryset=Group.objects.all(),
        required=False,
        empty_label='— Aucun groupe —',
        label='Groupe de permissions',
    )
    new_password1 = forms.CharField(
        required=False,
        label='Nouveau mot de passe',
        help_text='Laissez les deux champs vides pour conserver le mot de passe actuel.',
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'new-password'}),
    )
    new_password2 = forms.CharField(
        required=False,
        label='Confirmer le nouveau mot de passe',
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'new-password'}),
    )

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'username', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        self.fields['email'].required = True
        _block_browser_autofill(self, lock_text_fields=True)
        # Pré-remplir les champs supplémentaires depuis l'instance
        if self.instance.pk:
            self.fields['is_staff'].initial = self.instance.is_staff
            self.fields['groupe'].initial = self.instance.groups.first()
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column('first_name', css_class='col-md-6'),
                Column('last_name',  css_class='col-md-6'),
            ),
            Row(
                Column('username', css_class='col-md-6'),
                Column('email',    css_class='col-md-6'),
            ),
            Row(
                Column('new_password1', css_class='col-md-6'),
                Column('new_password2', css_class='col-md-6'),
            ),
            Row(
                Column('groupe',   css_class='col-md-8'),
                Column('is_staff', css_class='col-md-4 d-flex align-items-center pt-2'),
            ),
        )

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get('new_password1')
        password2 = cleaned_data.get('new_password2')
        if password1 or password2:
            if password1 != password2:
                self.add_error('new_password2', 'Les deux mots de passe ne correspondent pas.')
            else:
                try:
                    password_validation.validate_password(password1, self.instance)
                except forms.ValidationError as error:
                    self.add_error('new_password1', error)
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        groupe = self.cleaned_data.get('groupe')
        # L'accès à l'interface Django admin n'est autorisé que pour le groupe Administration,
        # quelle que soit la case cochée côté client (la case est verrouillée par JS sinon).
        user.is_staff = bool(self.cleaned_data.get('is_staff', False)) and bool(groupe) and groupe.name == 'Administration'
        new_password = self.cleaned_data.get('new_password1')
        if new_password:
            user.set_password(new_password)
        if commit:
            user.save()
            user.groups.set([groupe] if groupe else [])
        return user


class EmployeForm(forms.ModelForm):
    class Meta:
        model = Employe
        exclude = ['user']
        widgets = {
            'naissance_at': forms.DateInput(
                attrs={'type': 'date', 'class': 'form-control'},
                format='%Y-%m-%d',
            ),
            'embauche_at': forms.DateInput(
                attrs={'type': 'date', 'class': 'form-control'},
                format='%Y-%m-%d',
            ),
            'salaire': forms.NumberInput(attrs={'class': 'no-spinner'}),
            'telephone': forms.TextInput(attrs={
                'inputmode': 'numeric',
                'pattern': '[0-9]*',
                'oninput': "this.value = this.value.replace(/[^0-9]/g, '')",
            }),
            'observation': forms.Textarea(attrs={'rows': 3}),
        }

    def clean_telephone(self):
        telephone = self.cleaned_data.get('telephone')
        if telephone and not telephone.isdigit():
            raise forms.ValidationError('Le téléphone ne doit contenir que des chiffres.')
        return telephone

    def __init__(self, *args, hide_matricule_service=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['type_contrat'].widget = forms.Select(choices=TYPE_CONTRAT_CHOICES)
        _block_browser_autofill(self, lock_text_fields=True)
        self.fields['photo'].widget.attrs['accept'] = 'image/*'
        self.helper = FormHelper()
        self.helper.form_tag = False
        if hide_matricule_service:
            self.fields['matricule'].widget = forms.HiddenInput()
            self.fields['service'].widget = forms.HiddenInput()
            self.helper.layout = Layout(
                'matricule',
                'service',
                Row(
                    Column('type_contrat', css_class='col-md-4'),
                    Column('telephone',    css_class='col-md-4'),
                    Column('naissance_at', css_class='col-md-4'),
                ),
                Row(
                    Column('embauche_at', css_class='col-md-6'),
                    Column('salaire',     css_class='col-md-6'),
                ),
                'adresse',
                'photo',
                'observation',
            )
        else:
            self.helper.layout = Layout(
                Row(
                    Column('matricule',    css_class='col-md-4'),
                    Column('service',      css_class='col-md-4'),
                    Column('type_contrat', css_class='col-md-4'),
                ),
                Row(
                    Column('telephone',    css_class='col-md-4'),
                    Column('naissance_at', css_class='col-md-4'),
                    Column('embauche_at',  css_class='col-md-4'),
                ),
                Row(
                    Column('salaire', css_class='col-md-4'),
                    Column('adresse', css_class='col-md-8'),
                ),
                'photo',
                'observation',
            )


class UserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.get_full_name() or obj.username


class TransfereForm(forms.ModelForm):
    compte_source = UserChoiceField(
        queryset=User.objects.filter(is_active=True).order_by('last_name', 'first_name'),
        label='Compte source',
    )
    compte_destination = UserChoiceField(
        queryset=User.objects.filter(is_active=True).order_by('last_name', 'first_name'),
        label='Compte destination',
    )

    class Meta:
        model = Transfere
        fields = ['transfere_at', 'libelle', 'compte_source', 'compte_destination', 'montant']
        widgets = {
            'transfere_at': forms.DateInput(
                attrs={'type': 'date', 'class': 'form-control'},
                format='%Y-%m-%d',
            ),
            'libelle': forms.TextInput(attrs={
                'list': 'libelles-transfere',
                'autocomplete': 'off',
                # Classe visuelle uniquement : ajoute le chevron du form-select
                # de Bootstrap sur ce champ texte (voir combobox-input en CSS).
                'class': 'combobox-input',
            }),
            'montant': forms.NumberInput(attrs={'step': '0.001', 'min': '0.001', 'class': 'no-spinner'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['libelle'].required = True
        # Rendu manuel dans le template : pas de saisie automatique du navigateur
        # ni de liste de suggestions.
        anti_auto = {
            'autocomplete': 'new-password', 'aria-autocomplete': 'none',
            'data-lpignore': 'true', 'data-1p-ignore': 'true', 'data-bwignore': 'true',
        }
        for name, css in (('transfere_at', 'form-control'), ('libelle', 'form-control'),
                          ('montant', 'form-control no-spinner')):
            w = self.fields[name].widget
            w.attrs.pop('list', None)
            w.attrs.update(anti_auto, **{'class': css, 'id': 'trf-' + name})
        for name in ('compte_source', 'compte_destination'):
            self.fields[name].widget.attrs.update(
                {'class': 'form-select', 'autocomplete': 'off', 'id': 'trf-' + name},
            )
        self.helper = FormHelper()
        self.helper.form_tag = False


class GouvernoratForm(forms.ModelForm):
    class Meta:
        model = Gouvernorat
        fields = ['nom']


class DelegationForm(forms.ModelForm):
    class Meta:
        model = Delegation
        fields = ['gouvernorat', 'nom_delegation']


class ZoneForm(forms.ModelForm):
    class Meta:
        model = Zone
        fields = ['delegation', 'nom']


class JourNonOuvreForm(forms.ModelForm):
    class Meta:
        model = JourNonOuvre
        fields = ['date', 'libelle', 'type_jour', 'concerne_livraison']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
            'libelle': forms.TextInput(attrs={'class': 'form-control'}),
            'type_jour': forms.Select(attrs={'class': 'form-select'}),
            'concerne_livraison': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class AbsenceCommercialForm(forms.ModelForm):
    user = UserChoiceField(
        queryset=User.objects.filter(groups__name='Commercial', is_active=True).order_by('last_name', 'first_name'),
        label='Commercial',
    )

    class Meta:
        model = AbsenceCommercial
        fields = ['user', 'date_debut', 'date_fin', 'motif']
        widgets = {
            'date_debut': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
            'date_fin': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
            'motif': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def clean(self):
        cleaned_data = super().clean()
        date_debut = cleaned_data.get('date_debut')
        date_fin = cleaned_data.get('date_fin')
        if date_debut and date_fin and date_fin < date_debut:
            self.add_error('date_fin', 'La date de fin doit être postérieure ou égale à la date de début.')
        return cleaned_data
