from django import forms
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row

from .models import MatierePremiere, MatierePremiereFamille, Produit


class ProduitForm(forms.ModelForm):
    class Meta:
        model = Produit
        fields = ['nom', 'taille', 'poids', 'prix_unitaire', 'stock', 'quantite_minimale', 'observation']
        widgets = {
            'poids':             forms.NumberInput(attrs={'class': 'no-spinner'}),
            'prix_unitaire':     forms.NumberInput(attrs={'class': 'no-spinner', 'step': '0.001'}),
            'stock':             forms.NumberInput(attrs={'class': 'no-spinner'}),
            'quantite_minimale': forms.NumberInput(attrs={'class': 'no-spinner'}),
            'observation':       forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column('nom',   css_class='col-md-8'),
                Column('taille', css_class='col-md-4'),
            ),
            Row(
                Column('poids',         css_class='col-md-4'),
                Column('prix_unitaire', css_class='col-md-4'),
                Column('stock',         css_class='col-md-2'),
                Column('quantite_minimale', css_class='col-md-2'),
            ),
            'observation',
        )


class FamilleForm(forms.ModelForm):
    class Meta:
        model = MatierePremiereFamille
        fields = ['famille', 'observation']


class MatierePremiereForm(forms.ModelForm):
    class Meta:
        model = MatierePremiere
        fields = ['nom', 'matiere_premiere_famille', 'unite', 'quantite', 'stock_minimum']
        widgets = {
            'quantite':      forms.NumberInput(attrs={'class': 'no-spinner', 'step': '0.001'}),
            'stock_minimum': forms.NumberInput(attrs={'class': 'no-spinner', 'step': '0.001'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column('nom', css_class='col-md-6'),
                Column('matiere_premiere_famille', css_class='col-md-6'),
            ),
            Row(
                Column('unite',         css_class='col-md-4'),
                Column('quantite',      css_class='col-md-4'),
                Column('stock_minimum', css_class='col-md-4'),
            ),
        )


class ProduitSemiFiniForm(forms.Form):
    """Valide les champs communs aux deux enregistrements (Produit +
    MatierePremiere) créés/modifiés pour un produit semi-fini. Le contrôle de
    doublon de nom (table Produit / table MatierePremiere) est fait à part,
    voir production/views.py:_nom_semi_fini_conflit — hors de portée d'un
    ModelForm puisqu'il porte sur deux modèles distincts."""
    nom = forms.CharField(max_length=100)
    matiere_premiere_famille = forms.ModelChoiceField(
        queryset=MatierePremiereFamille.objects.order_by('famille'),
        required=False, label='Famille',
    )
    unite = forms.ChoiceField(choices=[('', '—')] + list(MatierePremiere.UNITE_CHOICES), required=False)
    quantite = forms.DecimalField(max_digits=11, decimal_places=3, required=False)
    stock_minimum = forms.DecimalField(max_digits=11, decimal_places=3, required=False)

    def clean_nom(self):
        return self.cleaned_data['nom'].strip()

    def clean_quantite(self):
        return self.cleaned_data.get('quantite') or 0

    def clean_stock_minimum(self):
        return self.cleaned_data.get('stock_minimum') or 0
