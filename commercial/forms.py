from django import forms

from .models import Client, Fournisseur


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = [
            'raison_sociale', 'nom_client', 'adresse', 'zone', 'telephone',
            'photo', 'google_mape', 'a_visiter_apres', 'observation',
            'is_active', 'created_at',
        ]
        widgets = {
            'created_at': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
            'observation': forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'google_mape': forms.Textarea(attrs={'rows': 2, 'class': 'form-control'}),
            'adresse': forms.TextInput(attrs={'class': 'form-control'}),
            'raison_sociale': forms.TextInput(attrs={'class': 'form-control'}),
            'nom_client': forms.TextInput(attrs={'class': 'form-control'}),
            'telephone': forms.TextInput(attrs={
                'class': 'form-control',
                'inputmode': 'numeric',
                'pattern': '[0-9]*',
                'oninput': "this.value = this.value.replace(/[^0-9]/g, '')",
            }),
            'a_visiter_apres': forms.NumberInput(attrs={'class': 'form-control no-spinner'}),
            'zone': forms.Select(attrs={'class': 'form-select'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def clean_telephone(self):
        telephone = self.cleaned_data.get('telephone')
        if telephone and not telephone.isdigit():
            raise forms.ValidationError('Le téléphone ne doit contenir que des chiffres.')
        return telephone


class FournisseurForm(forms.ModelForm):
    class Meta:
        model = Fournisseur
        fields = [
            'raison_sociale', 'nom_contact', 'type_fournisseur', 'adresse', 'telephone',
            'email', 'tva', 'rib', 'observations', 'created_at',
        ]
        widgets = {
            'raison_sociale':   forms.TextInput(attrs={'class': 'form-control'}),
            'nom_contact':      forms.TextInput(attrs={'class': 'form-control'}),
            'type_fournisseur': forms.Select(attrs={'class': 'form-select'}),
            'adresse':          forms.TextInput(attrs={'class': 'form-control'}),
            'telephone':        forms.TextInput(attrs={
                'class': 'form-control',
                'inputmode': 'numeric',
                'pattern': '[0-9]*',
                'oninput': "this.value = this.value.replace(/[^0-9]/g, '')",
            }),
            'email':            forms.EmailInput(attrs={'class': 'form-control'}),
            'tva':              forms.TextInput(attrs={'class': 'form-control'}),
            'rib':              forms.TextInput(attrs={'class': 'form-control'}),
            'observations':     forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'created_at':       forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['type_fournisseur'].required = False
        self.fields['type_fournisseur'].choices = (
            [('', '— Divers (par défaut) —')] + list(Fournisseur.TYPE_CHOICES)
        )
        self.fields['raison_sociale'].required = True

    def clean_telephone(self):
        telephone = self.cleaned_data.get('telephone')
        if telephone and not telephone.isdigit():
            raise forms.ValidationError('Le téléphone ne doit contenir que des chiffres.')
        return telephone
