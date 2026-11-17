from django import forms
from django.core import signing
from django.core.exceptions import ValidationError


class CheckoutForm(forms.Form):
    customer_name = forms.CharField(label='Nama Pembeli',max_length=100,widget=forms.TextInput(attrs={'autocomplete':'name'}))
    whatsapp = forms.RegexField(label='Nomor WhatsApp',regex=r'^\+?\d{8,15}$',max_length=16,error_messages={'invalid':'Isi nomor 8–15 digit, boleh diawali +.'},widget=forms.TextInput(attrs={'type':'tel','autocomplete':'tel'}))
    quantity = forms.IntegerField(label='Jumlah Pesanan',min_value=1,max_value=999999,initial=1)
    checkout_token = forms.CharField(widget=forms.HiddenInput)

    def __init__(self,*args,product=None,**kwargs):
        self.product=product
        super().__init__(*args,**kwargs)

    def clean_checkout_token(self):
        token=self.cleaned_data['checkout_token']
        try:
            data=signing.loads(token,salt='checkout',max_age=3600)
            if data['product']!=self.product.pk:
                raise signing.BadSignature
        except (signing.BadSignature,KeyError,TypeError):
            raise forms.ValidationError('Form kedaluwarsa. Muat ulang halaman lalu coba kembali.')
        return token

    def clean(self):
        data=super().clean()
        if self.product.price is None or self.product.price<=0:
            raise forms.ValidationError('Harga produk belum tersedia.')
        if not self.product.inventory_item_id:
            raise forms.ValidationError('Produk belum terhubung dengan persediaan.')
        if not self.product.inventory_item.is_active:
            raise forms.ValidationError('Barang persediaan tidak aktif.')
        if data.get('quantity',0)>self.product.stok_tersedia:
            self.add_error('quantity','Stok tidak cukup untuk jumlah tersebut.')
        return data
