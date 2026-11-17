
from django.db import models, transaction
from django.db.models import Q, F, Sum
from django.utils import timezone
from django.core.validators import RegexValidator, MinValueValidator
from decimal import Decimal
from django.core.exceptions import ValidationError



class GuardedQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if self.model.__name__ in ('JournalEntry', 'GeneralLedger') and 'status' in kwargs:
            raise ValidationError('Posting harus melalui penyimpanan jurnal yang tervalidasi.')
        with transaction.atomic():
            rows = list(self)
            for obj in rows:
                for key, value in kwargs.items():
                    if hasattr(value, 'resolve_expression'):
                        raise ValidationError('Gunakan layanan transaksi untuk perubahan ekspresi.')
                    setattr(obj, key, value)
                obj.save()
        return len(rows)

    def delete(self):
        with transaction.atomic():
            rows = list(self)
            for obj in rows:
                obj.check_delete()
            return super().delete()

    def bulk_create(self, objs, **kwargs):
        if kwargs.get('ignore_conflicts') or kwargs.get('update_conflicts'):
            raise ValidationError('Konflik transaksi tidak boleh diabaikan.')
        with transaction.atomic():
            for obj in objs:
                obj.save(force_insert=True)
        return objs

    def bulk_update(self, objs, fields, **kwargs):
        with transaction.atomic():
            for obj in objs:
                obj.save(update_fields=fields)
        return len(objs)


class GuardedModel(models.Model):
    objects = GuardedQuerySet.as_manager()

    class Meta:
        abstract = True

    @transaction.atomic
    def save(self, *args, **kwargs):
        # Accept trailing zeroes without accepting precision loss (e.g. 3000.00 -> 3000).
        for field in self._meta.concrete_fields:
            value = getattr(self, field.attname, None)
            if isinstance(field, models.DecimalField) and value is not None:
                value = Decimal(str(value))
                if value.is_finite():
                    quantized = value.quantize(Decimal(10) ** -field.decimal_places)
                    if value == quantized:
                        setattr(self, field.attname, quantized)
        self.full_clean()
        return super().save(*args, **kwargs)

    def check_delete(self):
        name = type(self).__name__
        if name in ('InventoryMovement',) or (name in ('JournalEntry', 'GeneralLedger') and self.status == 'posted') or (name == 'JournalLine' and self.journal_entry.status == 'posted') or (name == 'Order' and (self.status == 'selesai' or self.sales_journal_id)) or (name == 'ProductionCost' and (self.journal_line_id or self.batch.status != 'growing')):
            raise ValidationError('Catatan yang sudah direalisasi tidak boleh dihapus.')

    def delete(self, *args, **kwargs):
        self.check_delete()
        return super().delete(*args, **kwargs)


class Product(GuardedModel):

    name = models.CharField(
        max_length=100,
        verbose_name="Nama Produk"
    )

    description = models.TextField(
        blank=True,
        verbose_name="Deskripsi"
    )

    price = models.DecimalField(
        max_digits=12,
        decimal_places=0,
        null=True,
        blank=True,
        verbose_name="Harga"
    )

    unit = models.CharField(
        max_length=20,
        default="ikat",
        verbose_name="Satuan"
    )

    stock = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="Stok"
    )

    inventory_item = models.OneToOneField(
        "InventoryItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="website_product",
        verbose_name="Barang Persediaan",
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Produk Aktif"
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.name

    @property
    def stok_tersedia(self):
        if hasattr(self,"available_stock"):
            return self.available_stock or Decimal("0")
        if not self.inventory_item_id:
            return 0

        from .services.inventory_service import hitung_stok

        hasil = hitung_stok(self.inventory_item)
        return hasil["stok_akhir"]

    class Meta:
        verbose_name = "Produk"
        verbose_name_plural = "Daftar Produk"
        ordering = ["name"]
        constraints = [models.CheckConstraint(condition=Q(price__isnull=True)|Q(price__gt=0),name='product_positive_price')]

    image = models.ImageField(
    upload_to='products/',
    blank=True,
    null=True,
    verbose_name="Foto Produk"
)

    def clean(self):
        super().clean()
        if self.price is not None and self.price <= 0:
            raise ValidationError({'price': 'Harga harus lebih dari nol.'})
        if self.inventory_item_id and (self.inventory_item.item_type != 'harvest' or self.inventory_item.unit != self.unit):
            raise ValidationError('Produk harus terkait hasil panen dengan satuan yang sama.')
        if self.pk and self.orders.filter(status='selesai').exists():
            old = Product.objects.get(pk=self.pk)
            if old.inventory_item_id != self.inventory_item_id or old.unit != self.unit:
                raise ValidationError('Hubungan persediaan produk yang sudah terjual tidak boleh diubah.')
class Order(GuardedModel):
    checkout_key = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    requires_reconciliation = models.BooleanField(default=False, editable=False, verbose_name='Perlu rekonsiliasi')

    class Status(models.TextChoices):
        WAITING = 'menunggu', 'Menunggu Konfirmasi'
        CONFIRMED = 'dikonfirmasi', 'Dikonfirmasi'
        PROCESSING = 'diproses', 'Diproses'
        COMPLETED = 'selesai', 'Selesai'
        CANCELLED = 'dibatalkan', 'Dibatalkan'

    customer_name = models.CharField(
        max_length=100,
        verbose_name="Nama Pembeli"
    )

    whatsapp = models.CharField(
        max_length=20,
        verbose_name="Nomor WhatsApp",
        validators=[RegexValidator(r"^\+?\d{8,15}$", "Isi nomor WhatsApp 8–15 digit, boleh diawali +.")]
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="orders"
    )

    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name="Jumlah Ikat"
    )

    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=0,
        verbose_name="Harga Saat Pemesanan"
    )

    total_price = models.DecimalField(
        max_digits=14,
        decimal_places=0,
        verbose_name="Total Harga"
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.WAITING
    )
        
    payment_method = models.CharField(
        max_length=10,
        choices=[
            ("cash", "Tunai"),
            ("credit", "Kredit"),
        ],
        null=True,
        blank=True,
        verbose_name="Metode Pembayaran",
    )

    stock_out_movement = models.OneToOneField(
        "InventoryMovement",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sales_order",
        verbose_name="Mutasi Stok Penjualan",
    )

    sales_journal = models.OneToOneField(
        "JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sales_order",
        verbose_name="Jurnal Penjualan",
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.customer_name

    class Meta:
        verbose_name = "Pesanan"
        verbose_name_plural = "Daftar Pesanan"
        ordering = ["-created_at"]    
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0,unit_price__gt=0,total_price__gt=0),name='order_positive_values'),models.CheckConstraint(condition=Q(total_price=F('quantity')*F('unit_price')),name='order_consistent_total'),models.CheckConstraint(condition=~Q(status='selesai')|Q(requires_reconciliation=True)|Q(sales_journal__isnull=False,stock_out_movement__isnull=False,payment_method__in=['cash','credit']),name='completed_order_sources')]

    def clean(self):
        super().clean()
        if self.quantity is not None and self.quantity < 1:
            raise ValidationError({'quantity': 'Jumlah minimal satu.'})
        if self.unit_price is not None and (self.unit_price <= 0 or self.total_price != self.unit_price * self.quantity):
            raise ValidationError('Harga harus positif dan total harus sesuai jumlah dikali harga.')
        if self.status == 'selesai' and not self.requires_reconciliation and not (self.sales_journal_id and self.stock_out_movement_id and self.payment_method):
            raise ValidationError('Gunakan aksi penyelesaian penjualan agar jurnal dan stok tercatat.')
        if self.pk:
            old = Order.objects.get(pk=self.pk)
            if old.status == 'selesai':
                fields = ('product_id','quantity','unit_price','total_price','payment_method','status','sales_journal_id','stock_out_movement_id','requires_reconciliation')
                if any(getattr(old,f) != getattr(self,f) for f in fields):
                    raise ValidationError('Nilai dan status pesanan selesai tidak boleh diubah atau dihapus.')


class Account(GuardedModel):

    class Category(models.TextChoices):
        ASSET = 'asset', 'Aset'
        LIABILITY = 'liability', 'Liabilitas'
        EQUITY = 'equity', 'Ekuitas'
        REVENUE = 'revenue', 'Pendapatan'
        EXPENSE = 'expense', 'Beban'

    class NormalBalance(models.TextChoices):
        DEBIT = 'debit', 'Debit'
        CREDIT = 'credit', 'Kredit'

    account_code = models.CharField(
        max_length=10,
        unique=True,
        verbose_name="Kode Akun"
    )

    account_name = models.CharField(
        max_length=150,
        verbose_name="Nama Akun"
    )

    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        verbose_name="Kategori"
    )

    normal_balance = models.CharField(
        max_length=10,
        choices=NormalBalance.choices,
        verbose_name="Saldo Normal"
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Akun Aktif"
    )

    def __str__(self):
        return f"{self.account_code} - {self.account_name}"

    class Meta:
        verbose_name = "Akun"
        verbose_name_plural = "Daftar Akun"
        ordering = ["account_code"]

    def clean(self):
        super().clean()
        if self.pk and self.journal_lines.exists():
            old = Account.objects.get(pk=self.pk)
            if any(getattr(old, f) != getattr(self, f) for f in ('account_code', 'account_name', 'category', 'normal_balance')):
                raise ValidationError('Makna akun yang sudah dipakai jurnal tidak boleh ditimpa.')



class AccountingPeriod(GuardedModel):
    is_legacy = models.BooleanField(default=False, editable=False, verbose_name='Arsip periode lama')
    class Status(models.TextChoices):
        OPEN = "open", "Terbuka"
        CLOSED = "closed", "Ditutup"

    period_name = models.CharField(
        max_length=50,
        unique=True,
        verbose_name="Nama Periode"
    )

    start_date = models.DateField(
        verbose_name="Tanggal Mulai"
    )

    end_date = models.DateField(
        verbose_name="Tanggal Selesai"
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.OPEN,
        verbose_name="Status Periode"
    )

    def __str__(self):
        return self.period_name

    class Meta:
        verbose_name = "Periode Akuntansi"
        verbose_name_plural = "Periode Akuntansi"
        ordering = ["-start_date"]
        permissions = [('view_financial_reports','Dapat melihat laporan keuangan')]
        constraints = [models.CheckConstraint(condition=Q(start_date__lte=F('end_date')),name='period_valid_dates')]

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError({'end_date': 'Tanggal selesai tidak boleh sebelum tanggal mulai.'})
        if not self.is_legacy and self.start_date and self.end_date and AccountingPeriod.objects.filter(is_legacy=False, start_date__lte=self.end_date, end_date__gte=self.start_date).exclude(pk=self.pk).exists():
            raise ValidationError('Periode aktif tidak boleh tumpang tindih.')
        if self.pk:
            old = AccountingPeriod.objects.get(pk=self.pk)
            if old.status == 'closed' and (old.status, old.start_date, old.end_date, old.is_legacy) != (self.status, self.start_date, self.end_date, self.is_legacy):
                raise ValidationError('Periode tertutup tidak boleh dibuka atau diubah rentangnya.')
            if self.journal_entries.exists() and (old.start_date, old.end_date) != (self.start_date, self.end_date):
                raise ValidationError('Rentang periode yang sudah digunakan tidak boleh diubah.')
            if old.status != self.status and self.status == 'closed' and self.journal_entries.filter(status='posted').exists() and not self.journal_entries.filter(status='posted',entry_type='closing').exists():
                raise ValidationError('Posting jurnal penutup terlebih dahulu sebelum menutup periode.')


class JournalEntry(GuardedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"

    class EntryType(models.TextChoices):
        OPENING = "opening", "Saldo Awal"
        GENERAL = "general", "Jurnal Umum"
        ADJUSTMENT = "adjustment", "Penyesuaian"
        CLOSING = "closing", "Penutup"

    entry_type = models.CharField(
        max_length=12,
        choices=EntryType.choices,
        default=EntryType.GENERAL,
        verbose_name="Jenis Jurnal"
    )

    entry_number = models.CharField(max_length=30, unique=True)
    accounting_period = models.ForeignKey(
        AccountingPeriod,
        on_delete=models.PROTECT,
        related_name="journal_entries"
    )
    entry_date = models.DateField()
    description = models.TextField()
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.accounting_period_id and (self.accounting_period.status == 'closed' or self.accounting_period.is_legacy):
            raise ValidationError('Periode sudah ditutup atau merupakan arsip historis.')
        if self.status == self.Status.POSTED and self.accounting_period_id:
            period = self.accounting_period
            if period.is_legacy or period.journal_entries.filter(status='posted',entry_type='closing').exclude(pk=self.pk).exists():
                raise ValidationError('Periode sudah ditutup atau merupakan arsip historis.')
            if self.pk and self.lines.filter(account__is_active=False).exists():
                raise ValidationError('Jurnal tidak boleh diposting ke akun nonaktif.')
            if self.entry_type == self.EntryType.CLOSING and self.pk:
                from .services.financial_report_service import validate_closing
                validate_closing(self)
        super().clean()

        if not self.accounting_period_id:
            return

        periode = self.accounting_period

        # Tanggal jurnal harus sesuai dengan periode akuntansi.
        if self.entry_date:
            if not (
                periode.start_date
                <= self.entry_date
                <= periode.end_date
            ):
                raise ValidationError({
                    "entry_date": "Tanggal jurnal berada di luar periode akuntansi."
                })

        if self.status == self.Status.POSTED:

            # Jurnal tidak boleh diposting ke periode tertutup.
            if periode.status != AccountingPeriod.Status.OPEN:
                raise ValidationError({
                    "status": "Jurnal tidak dapat diposting karena periode telah ditutup."
                })

            # Simpan sebagai Draft terlebih dahulu.
            if not self.pk:
                raise ValidationError({
                    "status": "Simpan jurnal sebagai Draft sebelum melakukan Posting."
                })

            baris_jurnal = list(self.lines.all())

            if len(baris_jurnal) < 2:
                raise ValidationError({
                    "status": "Jurnal harus mempunyai minimal dua baris."
                })

            total_debit = sum(
                (baris.debit for baris in baris_jurnal),
                Decimal("0.00")
            )

            total_kredit = sum(
                (baris.credit for baris in baris_jurnal),
                Decimal("0.00")
            )

            if (
                total_debit <= 0
                or total_debit != total_kredit
                or any(
                    baris.debit < 0
                    or baris.credit < 0
                    or (baris.debit > 0 and baris.credit > 0)
                    or (baris.debit == 0 and baris.credit == 0)
                    for baris in baris_jurnal
                )
            ):
                raise ValidationError({
                    "status": "Jurnal tidak valid. Debit dan kredit harus seimbang dan setiap baris hanya boleh memiliki satu sisi."
                })

    @transaction.atomic
    def save(self, *args, **kwargs):

        # Cegah perubahan pada jurnal yang sudah Posted.
        if self.pk:
            status_lama = (
                JournalEntry.objects
                .filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )

            if status_lama == self.Status.POSTED:
                raise ValidationError(
                    "Jurnal yang sudah Posted tidak boleh diubah."
                )

        self.full_clean()
        result = super().save(*args, **kwargs)
        if self.status == self.Status.POSTED and self.entry_type == self.EntryType.CLOSING:
            period = self.accounting_period
            period.status = AccountingPeriod.Status.CLOSED
            period.save(update_fields=['status'])
        return result

    def __str__(self):
        return self.entry_number

    class Meta:
        verbose_name = "Jurnal Umum"
        verbose_name_plural = "Jurnal Umum"
        ordering = ["-entry_date", "entry_number"]
        indexes = [models.Index(fields=['accounting_period','status','entry_type','entry_date'],name='journal_report_idx')]
        constraints = [models.UniqueConstraint(fields=['accounting_period'],condition=Q(status='posted',entry_type='closing'),name='one_posted_closing')]


class JournalLine(GuardedModel):
    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.CASCADE,
        related_name="lines"
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="journal_lines"
    )
    description = models.CharField(max_length=255, blank=True)
    debit = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=0
    )
    credit = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=0
    )

    def clean(self):
        super().clean()

        if self.debit is not None and self.credit is not None:
            if self.debit < 0 or self.credit < 0:
                raise ValidationError(
                    "Debit dan kredit tidak boleh negatif."
                )

            if (self.debit > 0) == (self.credit > 0):
                raise ValidationError(
                    "Setiap baris harus memiliki debit atau kredit, bukan keduanya."
                )

    @transaction.atomic
    def save(self, *args, **kwargs):

        # Cegah perubahan baris lama dari jurnal Posted.
        if self.pk:
            if JournalLine.objects.filter(
                pk=self.pk,
                journal_entry__status=JournalEntry.Status.POSTED
            ).exists():
                raise ValidationError(
                    "Baris jurnal Posted tidak boleh diubah."
                )

        # Cegah penambahan baris ke jurnal Posted.
        if self.journal_entry_id:
            if JournalEntry.objects.filter(
                pk=self.journal_entry_id,
                status=JournalEntry.Status.POSTED
            ).exists():
                raise ValidationError(
                    "Tidak boleh menambah baris ke jurnal Posted."
                )

        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):

        # Cegah penghapusan baris jurnal Posted.
        if JournalEntry.objects.filter(
            pk=self.journal_entry_id,
            status=JournalEntry.Status.POSTED
        ).exists():
            raise ValidationError(
                "Baris jurnal Posted tidak boleh dihapus."
            )

        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.account.account_code} - {self.account.account_name}"

    class Meta:
        verbose_name = "Baris Jurnal"
        verbose_name_plural = "Baris Jurnal"
        constraints = [models.CheckConstraint(condition=Q(debit__gt=0,credit=0)|Q(credit__gt=0,debit=0),name='line_one_positive_side')]


class GeneralLedger(JournalEntry):
    class Meta:
        proxy = True
        verbose_name = "Buku Besar"
        verbose_name_plural = "Buku Besar"

class InventoryItem(GuardedModel):
    class ItemType(models.TextChoices):
        SEED = "seed", "Bibit"
        SUPPLY = "supply", "Perlengkapan"
        HARVEST = "harvest", "Hasil Panen"

    item_code = models.CharField(max_length=30, unique=True)
    item_name = models.CharField(max_length=100)
    item_type = models.CharField(
        max_length=20,
        choices=ItemType.choices
    )
    unit = models.CharField(max_length=20)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.item_code} - {self.item_name}"

    class Meta:
        verbose_name = "Barang Persediaan"
        verbose_name_plural = "Daftar Persediaan"
        ordering = ["item_code"]


class InventoryMovement(GuardedModel):
    class MovementType(models.TextChoices):
        IN = "in", "Masuk"
        OUT = "out", "Keluar"

    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="movements"
    )
    movement_date = models.DateField()
    movement_type = models.CharField(
        max_length=10,
        choices=MovementType.choices
    )
    quantity = models.DecimalField(
        max_digits=15,
        decimal_places=3
    )
    unit_cost = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        default=0
    )
    reference = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)


    def clean(self):
        if self.movement_type == self.MovementType.IN and self.unit_cost is not None and self.unit_cost <= 0:
            raise ValidationError({'unit_cost': 'Biaya stok masuk harus lebih dari nol agar dapat dinilai dengan FIFO.'})
        if self.item_id and self.movement_date:
            if AccountingPeriod.objects.filter(is_legacy=False,status='closed',start_date__lte=self.movement_date,end_date__gte=self.movement_date).exists():
                raise ValidationError('Mutasi tidak boleh dicatat pada periode tertutup.')
            if InventoryMovement.objects.filter(item_id=self.item_id,movement_date__gt=self.movement_date).exclude(pk=self.pk).exists():
                raise ValidationError('Tanggal mundur ditolak karena sudah ada mutasi setelah tanggal tersebut.')
        super().clean()

        from decimal import Decimal
        from django.db.models import Sum

        nol = Decimal("0")

        if self.quantity is not None and self.quantity <= nol:
            raise ValidationError({
                "quantity": "Jumlah barang harus lebih dari nol."
            })

        if self.unit_cost is not None and self.unit_cost < nol:
            raise ValidationError({
                "unit_cost": "Biaya per unit tidak boleh negatif."
            })

        if (
            self.item_id
            and self.quantity is not None
            and self.quantity > nol
            and self.movement_type == self.MovementType.OUT
        ):
            mutasi = InventoryMovement.objects.filter(
                item_id=self.item_id
            )

            if self.pk:
                mutasi = mutasi.exclude(pk=self.pk)

            masuk = mutasi.filter(
                movement_type=self.MovementType.IN
            ).aggregate(total=Sum("quantity"))["total"] or nol

            keluar = mutasi.filter(
                movement_type=self.MovementType.OUT
            ).aggregate(total=Sum("quantity"))["total"] or nol

            stok_tersedia = masuk - keluar

            if self.quantity > stok_tersedia:
                raise ValidationError({
                    "quantity": (
                        f"Stok tidak cukup. "
                        f"Stok tersedia: {stok_tersedia}"
                    )
                })

    
    @transaction.atomic
    def save(self, *args, **kwargs):
        # Cegah perubahan mutasi yang sudah tersimpan.
        if self.pk:
            sudah_ada = InventoryMovement.objects.filter(
                pk=self.pk
            ).exists()

            if sudah_ada:
                raise ValidationError(
                    "Mutasi persediaan yang sudah tersimpan "
                    "tidak boleh diubah langsung."
                )

        # Jalankan validasi jumlah dan stok.
        self.full_clean()

        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Mutasi persediaan tidak boleh dihapus langsung."
        )


    def __str__(self):
        return f"{self.item.item_name} - {self.movement_type}"

    class Meta:
        verbose_name = "Mutasi Persediaan"
        verbose_name_plural = "Mutasi Persediaan"
        ordering = ["-movement_date", "-id"]
        indexes = [models.Index(fields=['item','movement_date','id'],name='inventory_date_idx')]
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0,unit_cost__gte=0),name='movement_valid_values'),models.CheckConstraint(condition=Q(movement_type='out')|Q(movement_type='in',unit_cost__gt=0),name='movement_in_positive_cost')]

class ProductionBatch(GuardedModel):
    harvest_journal = models.OneToOneField('JournalEntry', null=True, blank=True, on_delete=models.PROTECT, related_name='harvest_batch', editable=False)
    class Status(models.TextChoices):
        GROWING = "growing", "Dalam Budidaya"
        HARVESTED = "harvested", "Sudah Dipanen"
        FAILED = "failed", "Gagal Panen"

    batch_code = models.CharField(
        max_length=30,
        unique=True
    )
    planting_date = models.DateField()
    harvest_date = models.DateField(
        null=True,
        blank=True
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.GROWING
    )
    notes = models.TextField(blank=True)

    harvest_movement = models.OneToOneField(
        "InventoryMovement",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="production_batch_harvest",
        editable=False,
    )

    def clean(self):
        if self.pk:
            old = ProductionBatch.objects.get(pk=self.pk)
            if old.status in ('harvested','failed') and any(getattr(old,f) != getattr(self,f) for f in ('batch_code','status','planting_date','harvest_date','harvest_movement_id','harvest_journal_id')):
                raise ValidationError('Batch yang sudah selesai tidak boleh diubah.')
        super().clean()

        if (
            self.planting_date
            and self.harvest_date
            and self.harvest_date < self.planting_date
        ):
            raise ValidationError({
                "harvest_date": (
                    "Tanggal panen tidak boleh "
                    "lebih awal dari tanggal tanam."
                )
            })

        if (
            self.status == self.Status.HARVESTED
            and not self.harvest_date
        ):
            raise ValidationError({
                "harvest_date": (
                    "Tanggal panen wajib diisi "
                    "untuk batch yang sudah dipanen."
                )
            })

        if (
            self.status == self.Status.HARVESTED
            and not self.harvest_movement_id
        ):
            raise ValidationError({
                "status": (
                    "Status sudah dipanen hanya boleh ditetapkan "
                    "setelah mutasi hasil panen tercatat."
                )
            })

    @transaction.atomic
    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.batch_code

    class Meta:
        verbose_name = "Kelompok Tanam"
        verbose_name_plural = "Kelompok Tanam"
        ordering = ["-planting_date"]


class ProductionCost(GuardedModel):
    batch = models.ForeignKey(
        ProductionBatch,
        on_delete=models.PROTECT,
        related_name="costs"
    )
    cost_date = models.DateField()
    description = models.CharField(max_length=200)
    amount = models.DecimalField(
        max_digits=15,
        decimal_places=2
    )
    journal_line = models.ForeignKey(
        "JournalLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="production_cost_allocations"
    )

    def clean(self):
        if self.batch_id and self.batch.status != 'growing':
            raise ValidationError('Biaya batch yang sudah dipanen tidak boleh diubah atau ditambah.')
        if self.batch_id and self.cost_date and self.cost_date < self.batch.planting_date:
            raise ValidationError('Tanggal biaya tidak boleh sebelum tanggal tanam.')
        if self.journal_line_id:
            source = self.journal_line
            if source.journal_entry.status != 'posted' or source.debit <= 0 or source.account.account_code != '1080':
                raise ValidationError('Sumber biaya harus debit persediaan dalam proses (1080) pada jurnal Posted.')
            total = ProductionCost.objects.filter(journal_line_id=self.journal_line_id).exclude(pk=self.pk).aggregate(value=Sum('amount'))['value'] or Decimal('0')
            if self.amount is not None and total + self.amount > source.debit:
                raise ValidationError('Alokasi biaya melebihi nilai debit sumber jurnal.')
        if self.pk and ProductionCost.objects.filter(pk=self.pk,journal_line__isnull=False).exists():
            raise ValidationError('Biaya yang sudah terkait jurnal tidak boleh diubah.')
        super().clean()

        if self.amount is not None and self.amount <= 0:
            raise ValidationError({
                "amount": "Biaya produksi harus lebih dari nol."
            })

    @transaction.atomic
    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.batch.batch_code} - {self.description}"

    class Meta:
        verbose_name = "Biaya Produksi"
        verbose_name_plural = "Biaya Produksi"
        ordering = ["cost_date", "id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0),name='production_positive_cost')]

       
