from datetime import date
from decimal import Decimal
from django.contrib.admin.helpers import ActionForm
from django.contrib import admin, messages
from django.core.exceptions import ValidationError, ObjectDoesNotExist, PermissionDenied
from django.db import transaction, OperationalError
from django.db.models import Sum, Case, When, F, DecimalField
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponseRedirect
from django.urls import reverse
from .services.sales_service import selesaikan_penjualan
from .models import (
    Product,
    Order,
    Account,
    AccountingPeriod,
    JournalEntry,
    JournalLine,
    GeneralLedger,
    InventoryItem,
    InventoryMovement,
    ProductionBatch,
    ProductionCost,
)
from .services.inventory_service import hitung_stok
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.core.exceptions import ValidationError, ObjectDoesNotExist, PermissionDenied
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse

from .services.production_service import catat_panen
# ============================
# PRODUK
# ============================

class ProductAdminForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = '__all__'

    def clean_image(self):
        image = self.cleaned_data.get('image')
        if image and image.size > 2 * 1024 * 1024:
            raise forms.ValidationError('Ukuran foto maksimal 2 MB.')
        if image and hasattr(image,'image') and max(image.image.size) > 4096:
            raise forms.ValidationError('Sisi foto maksimal 4096 piksel.')
        return image


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductAdminForm
    list_display = (
        "name",
        "price",
        "stok_aktual",
        "unit",
        "is_active",
    )
    list_filter = ("is_active",)

    @admin.display(description="Stok Aktual")
    def stok_aktual(self, obj):
        if not obj.inventory_item_id:
            return "Belum terhubung"

        return f"{obj.available_stock or 0} {obj.unit}"

    search_fields = ("name",)
    exclude = ("stock",)

    def get_queryset(self,request):
        return super().get_queryset(request).select_related('inventory_item').annotate(available_stock=Sum(Case(When(inventory_item__movements__movement_type='in',then=F('inventory_item__movements__quantity')),default=-F('inventory_item__movements__quantity'),output_field=DecimalField())))


# ============================
# PESANAN
# ============================


class PenjualanActionForm(ActionForm):
    periode_jual = forms.ModelChoiceField(
        queryset=AccountingPeriod.objects.filter(status="open",is_legacy=False),
        required=False,
        label="Periode Penjualan",
    )

    tanggal_jual = forms.DateField(
        required=False,
        label="Tanggal Penjualan",
        widget=forms.DateInput(
            attrs={"type": "date"}
        ),
    )


class OrderAdminForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = "__all__"

    def clean_status(self):
        status_baru = self.cleaned_data["status"]

        status_lama = None
        if self.instance.pk:
            status_lama = (
                Order.objects.filter(pk=self.instance.pk)
                .values_list("status", flat=True)
                .first()
            )

        if (
            status_baru == Order.Status.COMPLETED
            and status_lama != Order.Status.COMPLETED
        ):
            raise forms.ValidationError(
                "Gunakan aksi 'Selesaikan penjualan' "
                "agar stok dan jurnal ikut dicatat."
            )

        if (
            status_lama == Order.Status.COMPLETED
            and status_baru != Order.Status.COMPLETED
        ):
            raise forms.ValidationError(
                "Pesanan selesai tidak boleh diubah "
                "statusnya secara manual."
            )

        return status_baru


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    exclude = ('checkout_key',)
    form = OrderAdminForm
    action_form = PenjualanActionForm

    list_display = (
        "id",
        "customer_name",
        "product",
        "quantity",
        "total_price",
        "payment_method",
        "requires_reconciliation",
        "status",
        "created_at",
    )

    list_filter = (
        "status",
        "payment_method",
    )

    search_fields = (
        "customer_name",
        "whatsapp",
    )

    readonly_fields = (
        "stock_out_movement",
        "sales_journal",
        "created_at", "requires_reconciliation",
    )

    def get_readonly_fields(self,request,obj=None):
        if obj and obj.status == "selesai":
            return tuple(f.name for f in self.model._meta.fields if f.name != 'checkout_key')
        return super().get_readonly_fields(request,obj)

    def has_delete_permission(self,request,obj=None):
        return False

    actions = ["proses_penjualan"]

    @admin.action(
        description="Selesaikan penjualan (1 pesanan)", permissions=["change"]
    )
    def proses_penjualan(self, request, queryset):
        if not request.user.has_perm("core.change_order"):
            raise PermissionDenied
        if queryset.count() != 1:
            self.message_user(
                request,
                "Pilih tepat satu pesanan.",
                level=messages.ERROR,
            )
            return

        periode_id = request.POST.get("periode_jual")
        tanggal_input = request.POST.get("tanggal_jual")

        if not periode_id or not tanggal_input:
            self.message_user(
                request,
                "Isi periode dan tanggal penjualan.",
                level=messages.ERROR,
            )
            return

        try:
            tanggal = date.fromisoformat(tanggal_input)

            order = queryset.first()

            selesaikan_penjualan(
                order_id=order.pk,
                periode_id=int(periode_id),
                tanggal_transaksi=tanggal,
            )

        except (
            ValidationError,
            ValueError,
            ObjectDoesNotExist, OperationalError,
        ) as error:
            self.message_user(
                request,
                f"Penjualan gagal: {error}",
                level=messages.ERROR,
            )
            return

        self.message_user(
            request,
            f"Pesanan #{order.pk} berhasil diselesaikan. "
            "Stok keluar dan jurnal penjualan tercatat.",
            level=messages.SUCCESS,
        )



# ============================
# DAFTAR AKUN / COA
# ============================

@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = (
        "account_code",
        "account_name",
        "category",
        "normal_balance",
        "is_active",
    )
    list_filter = ("category", "normal_balance", "is_active")
    search_fields = ("account_code", "account_name")
    ordering = ("account_code",)

    def has_delete_permission(self, request, obj=None):
        return False


# ============================
# PERIODE AKUNTANSI
# ============================

@admin.register(AccountingPeriod)
class AccountingPeriodAdmin(admin.ModelAdmin):
    readonly_fields = ("status", "is_legacy")

    def get_readonly_fields(self,request,obj=None):
        if obj and (obj.status == "closed" or obj.is_legacy):
            return tuple(f.name for f in self.model._meta.fields)
        return super().get_readonly_fields(request,obj)

    list_display = (
        "period_name",
        "start_date",
        "end_date",
        "status",
    )
    list_filter = ("status",)


# ============================
# VALIDASI BARIS JURNAL
# ============================

class JournalLineFormSet(BaseInlineFormSet):

    def clean(self):
        super().clean()

        if any(self.errors):
            return

        for form in self.forms:
            data = getattr(form, "cleaned_data", None)

            if not data or data.get("DELETE"):
                continue

            if not data.get("account"):
                continue

            debit = data.get("debit") or Decimal("0.00")
            credit = data.get("credit") or Decimal("0.00")

            if debit < 0 or credit < 0:
                raise ValidationError(
                    "Debit dan kredit tidak boleh negatif."
                )

            if debit > 0 and credit > 0:
                raise ValidationError(
                    "Satu baris tidak boleh berisi debit dan kredit sekaligus."
                )

            if debit == 0 and credit == 0:
                raise ValidationError(
                    "Setiap baris harus memiliki nilai debit atau kredit."
                )


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    formset = JournalLineFormSet
    extra = 2


# ============================
# POSTING JURNAL
# ============================

@admin.action(description="Posting jurnal Draft terpilih", permissions=["change"])
def posting_jurnal(modeladmin, request, queryset):
    if not request.user.has_perm("core.change_journalentry"):
        raise PermissionDenied

    berhasil = 0

    for jurnal in queryset:
        try:
            with transaction.atomic():

                data = (
                    JournalEntry.objects
                    .select_for_update()
                    .get(pk=jurnal.pk)
                )

                if data.status != JournalEntry.Status.DRAFT:
                    raise ValidationError(
                        "Jurnal bukan berstatus Draft."
                    )

                data.status = JournalEntry.Status.POSTED

                # Memanggil validasi di models.py
                data.save(update_fields=["status"])

                berhasil += 1

        except ValidationError as error:
            modeladmin.message_user(
                request,
                f"{jurnal.entry_number}: {'; '.join(error.messages)}",
                level=messages.ERROR,
            )

    if berhasil:
        modeladmin.message_user(
            request,
            f"{berhasil} jurnal berhasil diposting.",
            level=messages.SUCCESS,
        )


# ============================
# JURNAL UMUM
# ============================

@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):

    
    list_display = (
        "entry_number",
        "entry_date",
        "accounting_period",
        "entry_type",
        "status",
    )

    list_filter = (
        "entry_type",
        "status",
        "accounting_period",
    )

    search_fields = ("entry_number", "description")
    readonly_fields = ("status",)
    inlines = [JournalLineInline]
    actions = [posting_jurnal]

    def has_change_permission(self, request, obj=None):
        if obj and obj.status == JournalEntry.Status.POSTED:
            return False

        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == JournalEntry.Status.POSTED:
            return False

        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


# ============================
# BUKU BESAR
# ============================

@admin.register(GeneralLedger)
class GeneralLedgerAdmin(admin.ModelAdmin):

    def changelist_view(self, request, extra_context=None):
        return HttpResponseRedirect(
            reverse("general_ledger")
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ============================
# DATA PERSEDIAAN
# ============================

@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = (
        "item_code",
        "item_name",
        "item_type",
        "unit",
        "stok_akhir", 
        "is_active",
    )

    list_filter = ("item_type", "is_active")
    search_fields = ("item_code", "item_name")
    ordering = ("item_code",)
    @admin.display(description="Stok Saat Ini")
    def stok_akhir(self, obj):
        return f"{obj.available_stock or 0} {obj.unit}"

    def get_queryset(self,request):
        return super().get_queryset(request).annotate(available_stock=Sum(Case(When(movements__movement_type='in',then=F('movements__quantity')),default=-F('movements__quantity'),output_field=DecimalField())))

# ============================
# MUTASI PERSEDIAAN
# ============================

@admin.register(InventoryMovement)
class InventoryMovementAdmin(admin.ModelAdmin):
    list_display = (
        "movement_date",
        "item",
        "movement_type",
        "quantity",
        "unit_cost",
        "reference",
    )

    list_filter = ("movement_type", "movement_date")
    search_fields = ("item__item_name", "reference")
    ordering = ("-movement_date",)

    def has_change_permission(self, request, obj=None):
        if obj is not None:
            return False

        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

# ============================
# KELOMPOK TANAM BAYAM
# ============================

class FormCatatPanen(forms.Form):
    item = forms.ModelChoiceField(
        queryset=InventoryItem.objects.filter(
            item_type=InventoryItem.ItemType.HARVEST,
            is_active=True,
        ),
        label="Barang Hasil Panen",
    )

    tanggal_panen = forms.DateField(
        label="Tanggal Panen",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    jumlah_panen = forms.DecimalField(
        label="Jumlah Panen",
        max_digits=15,
        decimal_places=3,
        min_value=Decimal("0.001"),
        help_text="Gunakan satuan barang yang dipilih, misalnya kg.",
    )

    biaya_per_unit = forms.DecimalField(
        label="Biaya per Unit (Rp)",
        max_digits=15,
        decimal_places=2,
        min_value=Decimal("0.01"),
        help_text="Isi berdasarkan biaya produksi yang dapat dibuktikan.",
    )

@admin.register(ProductionBatch)
class ProductionBatchAdmin(admin.ModelAdmin):
    readonly_fields = ("harvest_movement", "harvest_journal")

    def get_readonly_fields(self,request,obj=None):
        if obj and obj.status != "growing":
            return tuple(f.name for f in self.model._meta.fields)
        return super().get_readonly_fields(request,obj)

    list_display = (
        "batch_code",
        "planting_date",
        "harvest_date",
        "status",
    )

    list_filter = ("status", "planting_date")
    search_fields = ("batch_code",)
    ordering = ("-planting_date",)

    actions = ("proses_panen_terpilih",)

    @admin.action(description="Catat hasil panen (pilih 1 batch)", permissions=["change"])
    def proses_panen_terpilih(self, request, queryset):
        if not request.user.has_perm("core.change_productionbatch"):
            raise PermissionDenied
        if queryset.count() != 1:
            self.message_user(
                request,
                "Pilih tepat satu kelompok tanam.",
                level=messages.ERROR,
            )
            return None

        batch = queryset.first()

        if (
            batch.status != ProductionBatch.Status.GROWING
            or batch.harvest_movement_id is not None
        ):
            self.message_user(
                request,
                "Kelompok tanam ini tidak dapat dipanen lagi.",
                level=messages.ERROR,
            )
            return None

        if request.POST.get("konfirmasi_panen") == "1":
            form = FormCatatPanen(request.POST)

            if form.is_valid():
                data = form.cleaned_data

                try:
                    catat_panen(
                        batch_id=batch.pk,
                        item_id=data["item"].pk,
                        tanggal_panen=data["tanggal_panen"],
                        jumlah_panen=data["jumlah_panen"],
                        biaya_per_unit=data["biaya_per_unit"],
                    )
                except (ValidationError, ObjectDoesNotExist, OperationalError) as error:
                    form.add_error(None, error)
                else:
                    self.message_user(
                        request,
                        "Hasil panen berhasil dicatat.",
                        level=messages.SUCCESS,
                    )
                    return HttpResponseRedirect(
                        reverse("admin:core_productionbatch_changelist")
                    )
        else:
            form = FormCatatPanen()

        context = {
            **self.admin_site.each_context(request),
            "title": "Catat Hasil Panen",
            "form": form,
            "batch": batch,
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
        }

        return TemplateResponse(
            request,
            "admin/core/productionbatch/confirm_harvest.html",
            context,
        )


# ============================
# BIAYA PRODUKSI BAYAM
# ============================

@admin.register(ProductionCost)
class ProductionCostAdmin(admin.ModelAdmin):
    def get_readonly_fields(self,request,obj=None):
        if obj and (obj.journal_line_id or obj.batch.status != "growing"):
            return tuple(f.name for f in self.model._meta.fields)
        return super().get_readonly_fields(request,obj)

    def has_delete_permission(self,request,obj=None):
        return False

    list_display = (
        "batch",
        "cost_date",
        "description",
        "amount",
        "journal_line",
    )

    list_filter = ("batch", "cost_date")
    search_fields = (
        "batch__batch_code",
        "description",
    )
    ordering = ("-cost_date",)

# ============================
# URUTAN MENU DJANGO ADMIN
# ============================

original_get_app_list = admin.site.get_app_list


def custom_get_app_list(request, app_label=None):

    app_list = original_get_app_list(request, app_label)

    urutan_core = {
        "Order": 1,
        "Product": 2,
        "Account": 3,
        "AccountingPeriod": 4,
        "JournalEntry": 5,
        "GeneralLedger": 6,
        "InventoryItem": 7,
        "InventoryMovement": 8,
        "ProductionBatch": 9,
        "ProductionCost": 10,
    }

    for app in app_list:
        if app["app_label"] == "core":
            app["models"].sort(
                key=lambda model: urutan_core.get(
                    model["object_name"],
                    99
                )
            )

    return app_list


admin.site.get_app_list = custom_get_app_list
