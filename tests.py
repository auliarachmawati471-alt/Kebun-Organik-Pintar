
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import (
    Product,
    Order,
    Account,
    AccountingPeriod,
    JournalEntry,
    JournalLine,
    InventoryItem,
    InventoryMovement,
    ProductionBatch,
    ProductionCost,
)
from .services.production_service import catat_panen
from .services.sales_service import selesaikan_penjualan


def prepare_harvest_cost(batch,amount=Decimal('300000.00')):
    """A harvest fixture includes the evidence now required by the existing service."""
    period,_=AccountingPeriod.objects.get_or_create(period_name='Tahun 2026',defaults={'start_date':date(2026,1,1),'end_date':date(2026,12,31)})
    wip,_=Account.objects.get_or_create(account_code='1080',defaults=dict(account_name='Persediaan Dalam Proses',category='asset',normal_balance='debit'))
    Account.objects.get_or_create(account_code='1050',defaults=dict(account_name='Persediaan Panen',category='asset',normal_balance='debit'))
    cash,_=Account.objects.get_or_create(account_code='1010',defaults=dict(account_name='Kas',category='asset',normal_balance='debit'))
    journal=JournalEntry.objects.create(entry_number=f'BIAYA-{batch.pk}',accounting_period=period,entry_date=batch.planting_date,description='Biaya produksi uji')
    line=JournalLine.objects.create(journal_entry=journal,account=wip,debit=amount)
    JournalLine.objects.create(journal_entry=journal,account=cash,credit=amount)
    journal.status='posted';journal.save()
    return ProductionCost.objects.create(batch=batch,cost_date=batch.planting_date,description='Biaya produksi yang dibuktikan',amount=amount,journal_line=line)

class JournalAdjustmentTests(TestCase):

    def setUp(self):
        # Data ini hanya dibuat di database pengujian.
        self.periode = AccountingPeriod.objects.create(
            period_name="Tahun 2026",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            status=AccountingPeriod.Status.OPEN,
        )

        self.kas = Account.objects.create(
            account_code="1010",
            account_name="Kas",
            category="asset",
            normal_balance="debit",
            is_active=True,
        )

        self.modal = Account.objects.create(
            account_code="3010",
            account_name="Modal Bapak Mujiono",
            category="equity",
            normal_balance="credit",
            is_active=True,
        )

        self.jurnal = JournalEntry.objects.create(
            entry_number="TEST-AJP-001",
            accounting_period=self.periode,
            entry_date=date(2026, 12, 31),
            description="Pengujian jurnal penyesuaian",
            entry_type=JournalEntry.EntryType.ADJUSTMENT,
            status=JournalEntry.Status.DRAFT,
        )

        self.baris_debit = JournalLine.objects.create(
            journal_entry=self.jurnal,
            account=self.kas,
            debit=Decimal("100000.00"),
            credit=Decimal("0.00"),
        )

        self.baris_kredit = JournalLine.objects.create(
            journal_entry=self.jurnal,
            account=self.modal,
            debit=Decimal("0.00"),
            credit=Decimal("100000.00"),
        )

    def posting_jurnal(self):
        self.jurnal.status = JournalEntry.Status.POSTED
        self.jurnal.save()

    def test_jurnal_awalnya_draft(self):
        self.assertEqual(
            self.jurnal.status,
            JournalEntry.Status.DRAFT
        )

    def test_jenis_jurnal_penyesuaian(self):
        self.assertEqual(
            self.jurnal.entry_type,
            JournalEntry.EntryType.ADJUSTMENT
        )

    def test_jurnal_seimbang_bisa_posting(self):
        self.posting_jurnal()

        self.jurnal.refresh_from_db()

        self.assertEqual(
            self.jurnal.status,
            JournalEntry.Status.POSTED
        )

    def test_jurnal_tidak_seimbang_ditolak(self):
        self.baris_kredit.credit = Decimal("90000.00")
        self.baris_kredit.save()

        with self.assertRaises(ValidationError):
            self.posting_jurnal()

        self.jurnal.refresh_from_db()

        self.assertEqual(
            self.jurnal.status,
            JournalEntry.Status.DRAFT
        )

    def test_periode_ditutup_tidak_bisa_posting(self):
        self.periode.status = AccountingPeriod.Status.CLOSED
        self.periode.save()

        with self.assertRaises(ValidationError):
            self.posting_jurnal()

    def test_jurnal_posted_tidak_bisa_diubah(self):
        self.posting_jurnal()

        self.jurnal.description = "Perubahan tidak sah"

        with self.assertRaises(ValidationError):
            self.jurnal.save()

    def test_baris_posted_tidak_bisa_diubah(self):
        self.posting_jurnal()

        self.baris_debit.debit = Decimal("200000.00")

        with self.assertRaises(ValidationError):
            self.baris_debit.save()

    def test_baris_posted_tidak_bisa_dihapus(self):
        self.posting_jurnal()

        with self.assertRaises(ValidationError):
            self.baris_debit.delete()

    def test_tidak_bisa_tambah_baris_ke_posted(self):
        self.posting_jurnal()

        with self.assertRaises(ValidationError):
            JournalLine.objects.create(
                journal_entry=self.jurnal,
                account=self.kas,
                debit=Decimal("50000.00"),
                credit=Decimal("0.00"),
            )

    def test_penyesuaian_masuk_nssp_tapi_tidak_ns(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        user = User.objects.create_user(
            username="penguji_nssp",
            password="test123",
            is_staff=True,
        )
        from django.contrib.auth.models import Permission
        user.user_permissions.add(Permission.objects.get(codename="view_financial_reports",content_type__app_label="core"))
        self.client.force_login(user)

        # Posting jurnal penyesuaian dari data pengujian.
        self.posting_jurnal()

        # Neraca Saldo sebelum penyesuaian.
        response_ns = self.client.get(reverse("trial_balance"))

        # Neraca Saldo setelah penyesuaian.
        response_nssp = self.client.get(
            reverse("adjusted_trial_balance")
        )

        self.assertEqual(response_ns.status_code, 200)
        self.assertEqual(response_nssp.status_code, 200)

        # Jurnal adjustment tidak boleh masuk NS biasa.
        self.assertEqual(
            response_ns.context["total_debit"],
            Decimal("0.00")
        )

        # Jurnal adjustment harus masuk NSSP.
        self.assertEqual(
            response_nssp.context["total_debit"],
            Decimal("100000.00")
        )
        self.assertEqual(
            response_nssp.context["total_credit"],
            Decimal("100000.00")
        )

    def test_penyesuaian_masuk_neraca_lajur(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        user = User.objects.create_user(
            username="penguji_worksheet",
            password="test123",
            is_staff=True,
        )
        from django.contrib.auth.models import Permission
        user.user_permissions.add(Permission.objects.get(codename="view_financial_reports",content_type__app_label="core"))
        self.client.force_login(user)

        # Posting jurnal penyesuaian di database pengujian.
        self.posting_jurnal()

        response = self.client.get(reverse("worksheet"))

        self.assertEqual(response.status_code, 200)

        totals = response.context["totals"]

        # Neraca Saldo biasa tidak menghitung penyesuaian.
        self.assertEqual(totals["ns_debit"], Decimal("0.00"))
        self.assertEqual(totals["ns_kredit"], Decimal("0.00"))

        # Kolom penyesuaian menghitung jurnal adjustment.
        self.assertEqual(totals["adj_debit"], Decimal("100000.00"))
        self.assertEqual(totals["adj_kredit"], Decimal("100000.00"))

        # NSSP sudah memasukkan penyesuaian.
        self.assertEqual(totals["nssp_debit"], Decimal("100000.00"))
        self.assertEqual(totals["nssp_kredit"], Decimal("100000.00"))

        self.assertTrue(response.context["is_balanced"])

    def test_pengelompokan_laba_rugi_dan_neraca(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        # Buat akun tambahan dari COA untuk pengujian.
        penjualan = Account.objects.create(
            account_code="4010",
            account_name="Penjualan Bayam",
            category="revenue",
            normal_balance="credit",
            is_active=True,
        )

        beban_sewa = Account.objects.create(
            account_code="6020",
            account_name="Beban Sewa",
            category="expense",
            normal_balance="debit",
            is_active=True,
        )

        prive = Account.objects.create(
            account_code="3020",
            account_name="Prive Bapak Mujiono",
            category="equity",
            normal_balance="debit",
            is_active=True,
        )

        # Transaksi simulasi hanya untuk database test.
        jurnal = JournalEntry.objects.create(
            entry_number="TEST-JU-002",
            accounting_period=self.periode,
            entry_date=date(2026, 12, 31),
            description="Pengujian pengelompokan akun",
            entry_type=JournalEntry.EntryType.GENERAL,
            status=JournalEntry.Status.DRAFT,
        )

        transaksi = [
            (self.kas, "80000.00", "0.00"),
            (penjualan, "0.00", "80000.00"),
            (beban_sewa, "30000.00", "0.00"),
            (self.kas, "0.00", "30000.00"),
            (prive, "10000.00", "0.00"),
            (self.kas, "0.00", "10000.00"),
        ]

        for akun, debit, kredit in transaksi:
            JournalLine.objects.create(
                journal_entry=jurnal,
                account=akun,
                debit=Decimal(debit),
                credit=Decimal(kredit),
            )

        jurnal.status = JournalEntry.Status.POSTED
        jurnal.save()

        # Posting jurnal penyesuaian dari setUp().
        self.posting_jurnal()

        user = User.objects.create_user(
            username="penguji_laporan",
            password="test123",
            is_staff=True,
        )
        from django.contrib.auth.models import Permission
        user.user_permissions.add(Permission.objects.get(codename="view_financial_reports",content_type__app_label="core"))
        self.client.force_login(user)

        response = self.client.get(reverse("worksheet"))

        self.assertEqual(response.status_code, 200)

        rows = {
            row["account_code"]: row
            for row in response.context["rows"]
            if not row["is_result"]
        }

        # Pendapatan harus berada di Laba Rugi.
        self.assertEqual(
            rows["4010"]["lr_kredit"],
            Decimal("80000.00")
        )

        # Beban harus berada di Laba Rugi.
        self.assertEqual(
            rows["6020"]["lr_debit"],
            Decimal("30000.00")
        )

        # Prive masuk Neraca, bukan Laba Rugi.
        self.assertEqual(
            rows["3020"]["neraca_debit"],
            Decimal("10000.00")
        )
        self.assertEqual(
            rows["3020"]["lr_debit"],
            Decimal("0.00")
        )

        # Laba sementara = Pendapatan - Beban.
        self.assertEqual(
            response.context["hasil_periode"],
            Decimal("50000.00")
        )

        # Setelah baris laba, Neraca Lajur harus seimbang.
        self.assertTrue(response.context["is_balanced"])

class PembelianNetoTests(TestCase):

    def test_perhitungan_pembelian_neto(self):
        from .services.hpp_service import hitung_pembelian_neto

        periode = AccountingPeriod.objects.create(
            period_name="Tahun 2026",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            status=AccountingPeriod.Status.OPEN,
        )

        def buat_akun(kode, nama, kategori, normal):
            return Account.objects.create(
                account_code=kode,
                account_name=nama,
                category=kategori,
                normal_balance=normal,
                is_active=True,
            )

        kas = buat_akun("1010", "Kas", "asset", "debit")
        pembelian = buat_akun("5010", "Pembelian", "expense", "debit")
        angkut = buat_akun("5020", "Beban Angkut Pembelian", "expense", "debit")
        retur = buat_akun("5030", "Retur Pembelian", "expense", "credit")
        potongan = buat_akun("5040", "Potongan Pembelian", "expense", "credit")

        def buat_jurnal(nomor, akun, debit, kredit):
            jurnal = JournalEntry.objects.create(
                entry_number=nomor,
                accounting_period=periode,
                entry_date=date(2026, 6, 30),
                description="Simulasi pembelian",
                entry_type=JournalEntry.EntryType.GENERAL,
                status=JournalEntry.Status.DRAFT,
            )

            JournalLine.objects.create(
                journal_entry=jurnal,
                account=akun,
                debit=Decimal(debit),
                credit=Decimal(kredit),
            )

            JournalLine.objects.create(
                journal_entry=jurnal,
                account=kas,
                debit=Decimal(kredit),
                credit=Decimal(debit),
            )

            jurnal.status = JournalEntry.Status.POSTED
            jurnal.save()

        buat_jurnal("TEST-001", pembelian, "5000000", "0")
        buat_jurnal("TEST-002", angkut, "200000", "0")
        buat_jurnal("TEST-003", retur, "0", "300000")
        buat_jurnal("TEST-004", potongan, "0", "100000")

        hasil = hitung_pembelian_neto(periode)

        self.assertEqual(
            hasil["pembelian_neto"],
            Decimal("4800000.00")
        )

class InventoryMovementTests(TestCase):

    def test_perhitungan_stok_otomatis(self):
        from .services.inventory_service import hitung_stok

        self.buat_mutasi(
            InventoryMovement.MovementType.IN,
            "10.000",
        )

        self.buat_mutasi(
            InventoryMovement.MovementType.IN,
            "5.000",
        )

        self.buat_mutasi(
            InventoryMovement.MovementType.OUT,
            "4.000",
        )

        hasil = hitung_stok(self.bibit)

        self.assertEqual(hasil["masuk"], Decimal("15.000"))
        self.assertEqual(hasil["keluar"], Decimal("4.000"))
        self.assertEqual(hasil["stok_akhir"], Decimal("11.000"))

    def setUp(self):
        # Barang simulasi khusus database pengujian.
        self.bibit = InventoryItem.objects.create(
            item_code="TEST-BIBIT-001",
            item_name="Bibit Bayam Uji",
            item_type=InventoryItem.ItemType.SEED,
            unit="gram",
            is_active=True,
        )

    def buat_mutasi(self, jenis, jumlah, biaya="1000.00"):
        return InventoryMovement.objects.create(
            item=self.bibit,
            movement_date=date(2026, 6, 1),
            movement_type=jenis,
            quantity=Decimal(jumlah),
            unit_cost=Decimal(biaya),
            reference="TEST-STOK",
        )

    def test_barang_masuk_berhasil(self):
        mutasi = self.buat_mutasi(
            InventoryMovement.MovementType.IN,
            "10.000",
        )

        self.assertEqual(
            mutasi.quantity,
            Decimal("10.000")
        )

    def test_jumlah_nol_ditolak(self):
        with self.assertRaises(ValidationError):
            self.buat_mutasi(
                InventoryMovement.MovementType.IN,
                "0.000",
            )

    def test_jumlah_negatif_ditolak(self):
        with self.assertRaises(ValidationError):
            self.buat_mutasi(
                InventoryMovement.MovementType.IN,
                "-5.000",
            )

    def test_biaya_negatif_ditolak(self):
        with self.assertRaises(ValidationError):
            self.buat_mutasi(
                InventoryMovement.MovementType.IN,
                "10.000",
                "-1000.00",
            )

    def test_barang_keluar_tanpa_stok_ditolak(self):
        with self.assertRaises(ValidationError):
            self.buat_mutasi(
                InventoryMovement.MovementType.OUT,
                "5.000",
            )

    def test_barang_keluar_valid(self):
        # Stok masuk 10 gram.
        self.buat_mutasi(
            InventoryMovement.MovementType.IN,
            "10.000",
        )

        # Keluar 4 gram, masih diperbolehkan.
        mutasi = self.buat_mutasi(
            InventoryMovement.MovementType.OUT,
            "4.000",
        )

        self.assertEqual(
            mutasi.quantity,
            Decimal("4.000")
        )

    def test_barang_keluar_melebihi_stok_ditolak(self):
        # Stok tersedia 10 gram.
        self.buat_mutasi(
            InventoryMovement.MovementType.IN,
            "10.000",
        )

        # Mencoba mengeluarkan 15 gram.
        with self.assertRaises(ValidationError):
            self.buat_mutasi(
                InventoryMovement.MovementType.OUT,
                "15.000",
            )

    def test_mutasi_tersimpan_tidak_bisa_diubah(self):
        # Buat transaksi barang masuk.
        mutasi = self.buat_mutasi(
            InventoryMovement.MovementType.IN,
            "10.000",
        )

        # Coba mengubah transaksi yang sudah tersimpan.
        mutasi.quantity = Decimal("20.000")

        with self.assertRaises(ValidationError):
            mutasi.save()

        # Pastikan data asli tidak berubah.
        mutasi.refresh_from_db()

        self.assertEqual(
            mutasi.quantity,
            Decimal("10.000")
        )

    def test_mutasi_tersimpan_tidak_bisa_dihapus(self):
        # Buat transaksi barang masuk.
        mutasi = self.buat_mutasi(
            InventoryMovement.MovementType.IN,
            "10.000",
        )

        # Coba menghapus transaksi.
        with self.assertRaises(ValidationError):
            mutasi.delete()

        # Pastikan transaksi masih tersimpan.
        self.assertTrue(
            InventoryMovement.objects.filter(
                pk=mutasi.pk
            ).exists()
        )

class ProductionTests(TestCase):

    def setUp(self):
        self.batch = ProductionBatch.objects.create(
            batch_code="TEST-BAYAM-001",
            planting_date=date(2026, 1, 8),
            status=ProductionBatch.Status.GROWING,
        )

    def test_tanggal_panen_tidak_boleh_sebelum_tanam(self):
        with self.assertRaises(ValidationError):
            ProductionBatch.objects.create(
                batch_code="TEST-BAYAM-002",
                planting_date=date(2026, 1, 10),
                harvest_date=date(2026, 1, 5),
                status=ProductionBatch.Status.HARVESTED,
            )

    def test_sudah_panen_wajib_isi_tanggal(self):
        with self.assertRaises(ValidationError):
            ProductionBatch.objects.create(
                batch_code="TEST-BAYAM-003",
                planting_date=date(2026, 1, 8),
                status=ProductionBatch.Status.HARVESTED,
            )

    def test_biaya_produksi_valid(self):
        biaya = ProductionCost.objects.create(
            batch=self.batch,
            cost_date=date(2026, 1, 9),
            description="Pemakaian bibit bayam",
            amount=Decimal("50000.00"),
        )

        self.assertEqual(
            biaya.amount,
            Decimal("50000.00")
        )

    def test_biaya_produksi_nol_ditolak(self):
        with self.assertRaises(ValidationError):
            ProductionCost.objects.create(
                batch=self.batch,
                cost_date=date(2026, 1, 9),
                description="Biaya tidak valid",
                amount=Decimal("0.00"),
            )

    def test_biaya_produksi_negatif_ditolak(self):
        with self.assertRaises(ValidationError):
            ProductionCost.objects.create(
                batch=self.batch,
                cost_date=date(2026, 1, 9),
                description="Biaya tidak valid",
                amount=Decimal("-10000.00"),
            )

class CatatPanenTests(TestCase):

    def setUp(self):
        self.batch = ProductionBatch.objects.create(
            batch_code="TEST-PANEN-001",
            planting_date=date(2026, 1, 8),
            status=ProductionBatch.Status.GROWING,
        )

        self.bayam = InventoryItem.objects.create(
            item_code="TEST-HASIL-001",
            item_name="Bayam Hasil Panen",
            item_type=InventoryItem.ItemType.HARVEST,
            unit="kg",
            is_active=True,
        )

    def test_panen_berhasil_menambah_stok(self):
        prepare_harvest_cost(self.batch)
        hasil = catat_panen(
            batch_id=self.batch.id,
            item_id=self.bayam.id,
            tanggal_panen=date(2026, 2, 7),
            jumlah_panen="60.000",
            biaya_per_unit="5000.00",
        )

        self.assertEqual(
            hasil.status,
            ProductionBatch.Status.HARVESTED,
        )

        self.assertEqual(
            hasil.harvest_date,
            date(2026, 2, 7),
        )

        mutasi = InventoryMovement.objects.get(
            pk=hasil.harvest_movement_id
        )

        self.assertEqual(
            mutasi.movement_type,
            InventoryMovement.MovementType.IN,
        )

        self.assertEqual(
            mutasi.quantity,
            Decimal("60.000"),
        )

        self.assertEqual(
            mutasi.item_id,
            self.bayam.id,
        )

    def test_panen_ganda_ditolak(self):
        prepare_harvest_cost(self.batch)
        catat_panen(
            self.batch.id,
            self.bayam.id,
            date(2026, 2, 7),
            "60.000",
            "5000.00",
        )

        with self.assertRaises(ValidationError):
            catat_panen(
                self.batch.id,
                self.bayam.id,
                date(2026, 2, 8),
                "20.000",
                "5000.00",
            )

        self.assertEqual(
            InventoryMovement.objects.count(),
            1,
        )

    def test_tanggal_panen_sebelum_tanam_ditolak(self):
        with self.assertRaises(ValidationError):
            catat_panen(
                self.batch.id,
                self.bayam.id,
                date(2026, 1, 5),
                "60.000",
                "5000.00",
            )

        self.assertFalse(
            InventoryMovement.objects.exists()
        )

    def test_jumlah_panen_nol_ditolak(self):
        with self.assertRaises(ValidationError):
            catat_panen(
                self.batch.id,
                self.bayam.id,
                date(2026, 2, 7),
                "0.000",
                "5000.00",
            )

        self.assertFalse(
            InventoryMovement.objects.exists()
        )

    def test_bukan_barang_hasil_panen_ditolak(self):
        bibit = InventoryItem.objects.create(
            item_code="TEST-BIBIT-PANEN",
            item_name="Bibit Bayam",
            item_type=InventoryItem.ItemType.SEED,
            unit="gram",
        )

        with self.assertRaises(ValidationError):
            catat_panen(
                self.batch.id,
                bibit.id,
                date(2026, 2, 7),
                "60.000",
                "5000.00",
            )

        self.assertFalse(
            InventoryMovement.objects.exists()
        )

    def test_total_biaya_produksi(self):
        from .services.production_service import (
            hitung_biaya_produksi
        )

        ProductionCost.objects.create(
            batch=self.batch,
            cost_date=date(2026, 1, 9),
            description="Pemakaian bibit",
            amount=Decimal("50000.00"),
        )

        ProductionCost.objects.create(
            batch=self.batch,
            cost_date=date(2026, 1, 10),
            description="Biaya perawatan",
            amount=Decimal("30000.00"),
        )

        hasil = hitung_biaya_produksi(self.batch.id)

        self.assertEqual(
            hasil["total_biaya"],
            Decimal("80000.00"),
        )

        self.assertEqual(
            hasil["jumlah_komponen"],
            2,
        )

        self.assertEqual(
            hasil["belum_terhubung_jurnal"],
            2,
        )

class CatatPanenAdminTests(TestCase):

    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        admin_user = User.objects.create_superuser(
            username="admin_uji_panen",
            email="adminuji@example.com",
            password="password-khusus-pengujian",
        )
        self.client.force_login(admin_user)

        self.batch = ProductionBatch.objects.create(
            batch_code="TEST-ADMIN-PANEN",
            planting_date=date(2026, 1, 8),
            status=ProductionBatch.Status.GROWING,
        )

        self.bayam = InventoryItem.objects.create(
            item_code="TEST-ADMIN-BAYAM",
            item_name="Bayam Hasil Panen Uji",
            item_type=InventoryItem.ItemType.HARVEST,
            unit="kg",
        )

    def test_form_panen_admin_muncul(self):
        from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
        from django.urls import reverse

        url = reverse("admin:core_productionbatch_changelist")

        response = self.client.post(url, {
            "action": "proses_panen_terpilih",
            "select_across": "0",
            ACTION_CHECKBOX_NAME: str(self.batch.pk),
        })

        self.assertEqual(response.status_code, 200)

        self.assertTemplateUsed(
            response,
            "admin/core/productionbatch/confirm_harvest.html",
        )

    def test_panen_admin_menambah_stok(self):
        prepare_harvest_cost(self.batch)
        from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
        from django.urls import reverse

        url = reverse("admin:core_productionbatch_changelist")

        response = self.client.post(url, {
            "action": "proses_panen_terpilih",
            "select_across": "0",
            ACTION_CHECKBOX_NAME: str(self.batch.pk),
            "konfirmasi_panen": "1",
            "item": str(self.bayam.pk),
            "tanggal_panen": "2026-02-07",
            "jumlah_panen": "60.000",
            "biaya_per_unit": "5000.00",
        })

        self.assertEqual(response.status_code, 302)

        self.batch.refresh_from_db()

        self.assertEqual(
            self.batch.status,
            ProductionBatch.Status.HARVESTED,
        )

        mutasi = InventoryMovement.objects.get(
            pk=self.batch.harvest_movement_id,
        )

        self.assertEqual(
            mutasi.quantity,
            Decimal("60.000"),
        )

        self.assertEqual(
            mutasi.item_id,
            self.bayam.pk,
        )

class SelesaikanPenjualanTests(TestCase):

    def setUp(self):
        self.periode = AccountingPeriod.objects.create(
            period_name="November 2026 Uji",
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),
            status="open",
        )

        for kode, nama, kategori, saldo_normal in [
            ("1010", "Kas", "asset", "debit"),
            ("1030", "Piutang Usaha", "asset", "debit"),
            ("4010", "Penjualan Bayam", "revenue", "credit"),
        ]:
            Account.objects.create(
                account_code=kode,
                account_name=nama,
                category=kategori,
                normal_balance=saldo_normal,
                is_active=True,
            )

        self.item = InventoryItem.objects.create(
            item_code="TEST-STOK-JUAL",
            item_name="Bayam Hasil Panen Uji",
            item_type=InventoryItem.ItemType.HARVEST,
            unit="ikat",
            is_active=True,
        )

        InventoryMovement.objects.create(
            item=self.item,
            movement_date=date(2026, 11, 1),
            movement_type=InventoryMovement.MovementType.IN,
            quantity=Decimal("250.000"),
            unit_cost=Decimal("1800.00"),
            reference="STOK-AWAL-UJI",
        )

        self.produk = Product.objects.create(
            name="Bayam Hijau Uji",
            description="Produk untuk pengujian",
            price=Decimal("3000.00"),
            unit="ikat",
            stock=Decimal("2250.00"),
            inventory_item=self.item,
            is_active=True,
        )

    def buat_order(self, jumlah, pembayaran):
        return Order.objects.create(
            customer_name="Pelanggan Pengujian",
            whatsapp="081234567890",
            product=self.produk,
            quantity=jumlah,
            unit_price=Decimal("3000.00"),
            total_price=Decimal("3000.00") * jumlah,
            status="diproses",
            payment_method=pembayaran,
        )

    def test_penjualan_tunai_mengurangi_stok(self):
        from .services.inventory_service import hitung_stok

        order = self.buat_order(150, "cash")

        hasil = selesaikan_penjualan(
            order.id,
            self.periode.id,
            date(2026, 11, 10),
        )

        self.assertEqual(hasil.status, "selesai")
        self.assertIsNotNone(hasil.stock_out_movement_id)
        self.assertIsNotNone(hasil.sales_journal_id)

        self.assertEqual(
            hitung_stok(self.item)["stok_akhir"],
            Decimal("100.000"),
        )

        self.assertEqual(
            hasil.sales_journal.status,
            JournalEntry.Status.POSTED,
        )

        self.assertTrue(
            JournalLine.objects.filter(
                journal_entry=hasil.sales_journal,
                account__account_code="1010",
                debit=Decimal("450000.00"),
            ).exists()
        )

    def test_penjualan_kredit_mencatat_piutang(self):
        order = self.buat_order(80, "credit")

        hasil = selesaikan_penjualan(
            order.id,
            self.periode.id,
            date(2026, 11, 11),
        )

        self.assertTrue(
            JournalLine.objects.filter(
                journal_entry=hasil.sales_journal,
                account__account_code="1030",
                debit=Decimal("240000.00"),
            ).exists()
        )

        self.assertTrue(
            JournalLine.objects.filter(
                journal_entry=hasil.sales_journal,
                account__account_code="4010",
                credit=Decimal("240000.00"),
            ).exists()
        )

    def test_penjualan_ganda_ditolak(self):
        order = self.buat_order(150, "cash")

        selesaikan_penjualan(
            order.id,
            self.periode.id,
            date(2026, 11, 10),
        )

        with self.assertRaises(ValidationError):
            selesaikan_penjualan(
                order.id,
                self.periode.id,
                date(2026, 11, 10),
            )

        self.assertEqual(
            JournalEntry.objects.count(),
            1,
        )

        # Satu stok masuk awal + satu stok keluar.
        self.assertEqual(
            InventoryMovement.objects.count(),
            2,
        )

    def test_stok_tidak_cukup_ditolak(self):
        order = self.buat_order(251, "cash")

        with self.assertRaises(ValidationError):
            selesaikan_penjualan(
                order.id,
                self.periode.id,
                date(2026, 11, 10),
            )

        self.assertFalse(
            JournalEntry.objects.exists()
        )

        # Hanya mutasi stok awal yang tersimpan.
        self.assertEqual(
            InventoryMovement.objects.count(),
            1,
        )

    def test_penjualan_pada_periode_ditutup_ditolak(self):
        # Buat pesanan yang belum diselesaikan.
        order = self.buat_order(1, "cash")

        # Fixture memakai transisi status tervalidasi, bukan memaksa jurnal kosong Posted.
        self.periode.status=AccountingPeriod.Status.CLOSED
        self.periode.save()

        jumlah_mutasi_awal = InventoryMovement.objects.count()

        # Penjualan pada periode tertutup harus ditolak.
        with self.assertRaisesMessage(
            ValidationError,
            "Periode akuntansi sudah ditutup",
        ):
            selesaikan_penjualan(
                order.id,
                self.periode.id,
                date(2026, 11, 10),
            )

        # Pesanan tidak boleh berubah.
        order.refresh_from_db()

        self.assertEqual(order.status, "diproses")
        self.assertIsNone(order.stock_out_movement_id)
        self.assertIsNone(order.sales_journal_id)

        # Tidak boleh muncul pengurangan stok.
        self.assertEqual(
            InventoryMovement.objects.count(),
            jumlah_mutasi_awal,
        )

        # Tidak boleh muncul jurnal penjualan baru.
        self.assertFalse(
            JournalEntry.objects.filter(
                entry_number=f"PJ-ORDER-{order.id}"
            ).exists()
        )

    def test_aksi_admin_selesaikan_penjualan(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        from .services.inventory_service import hitung_stok

        admin_user = get_user_model().objects.create_superuser(
            username="admin_uji_penjualan",
            email="adminuji@example.com",
            password="PasswordUji123!",
        )

        self.client.force_login(admin_user)

        order = self.buat_order(150, "cash")

        url = reverse("admin:core_order_changelist")

        response = self.client.post(
            url,
            {
                "action": "proses_penjualan",
                "_selected_action": [str(order.pk)],
                "select_across": "0",
                "index": "0",
                "periode_jual": str(self.periode.pk),
                "tanggal_jual": "2026-11-10",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)

        order.refresh_from_db()

        self.assertEqual(order.status, "selesai")
        self.assertIsNotNone(order.stock_out_movement_id)
        self.assertIsNotNone(order.sales_journal_id)

        self.assertEqual(
            hitung_stok(self.item)["stok_akhir"],
            Decimal("100.000"),
        )

        self.assertEqual(
            order.sales_journal.status,
            JournalEntry.Status.POSTED,
        )
