from django import forms
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row

from .models import Produit


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
