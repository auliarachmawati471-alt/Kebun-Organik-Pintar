
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from core.models import (
    Account,
    AccountingPeriod,
    InventoryMovement,
    JournalEntry,
    JournalLine,
    Order,
)

from core.services.inventory_service import hitung_stok


@transaction.atomic
def selesaikan_penjualan(order_id, periode_id, tanggal_transaksi):
    """
    Menyelesaikan satu pesanan:
    1. Memeriksa stok
    2. Mencatat stok keluar
    3. Membuat jurnal penjualan Posted
    4. Mengubah pesanan menjadi Selesai
    """

    order = (
        Order.objects.select_for_update()
        .select_related("product__inventory_item")
        .get(pk=order_id)
    )

    # Hanya pesanan terkonfirmasi/diproses yang boleh diselesaikan.
    if order.status not in ("dikonfirmasi", "diproses"):
        raise ValidationError(
            "Pesanan harus dikonfirmasi atau diproses terlebih dahulu."
        )

    # Cegah pencatatan penjualan ganda.
    if order.stock_out_movement_id or order.sales_journal_id:
        raise ValidationError(
            "Pesanan ini sudah memiliki transaksi penjualan."
        )

    if order.payment_method not in ("cash", "credit"):
        raise ValidationError(
            "Metode pembayaran harus Tunai atau Kredit."
        )

    if not isinstance(tanggal_transaksi, date):
        raise ValidationError("Tanggal transaksi tidak valid.")

    periode = AccountingPeriod.objects.get(pk=periode_id)

    if not (
        periode.start_date <= tanggal_transaksi <= periode.end_date
    ):
        raise ValidationError(
            "Tanggal transaksi harus berada dalam periode yang dipilih."
        )

    # Cegah penjualan pada periode yang sudah ditutup.
    periode_sudah_ditutup = JournalEntry.objects.filter(
        accounting_period=periode,
        entry_type=JournalEntry.EntryType.CLOSING,
        status=JournalEntry.Status.POSTED,
    ).exists()

    if periode_sudah_ditutup:
        raise ValidationError(
            "Periode akuntansi sudah ditutup. "
            "Penjualan tidak dapat dicatat "
            "pada periode ini."
        )

    if periode.status != AccountingPeriod.Status.OPEN or periode.is_legacy:
        raise ValidationError('Periode akuntansi sudah ditutup atau diarsipkan.')
    if not order.product.inventory_item_id:
        raise ValidationError('Produk belum terhubung dengan persediaan.')
    if not order.product.is_active or not order.product.inventory_item.is_active:
        raise ValidationError('Produk dan barang persediaan harus aktif.')
    order.full_clean()

    item = order.product.inventory_item
    jumlah = Decimal(str(order.quantity))
    stok = hitung_stok(item)["stok_akhir"]

    if jumlah <= 0 or jumlah > stok:
        raise ValidationError(
            f"Stok tidak mencukupi. Tersedia {stok} {item.unit}."
        )

    nilai_penjualan = order.total_price

    if nilai_penjualan is None or nilai_penjualan <= 0:
        raise ValidationError("Nilai penjualan tidak valid.")

    if nilai_penjualan != order.unit_price * order.quantity:
        raise ValidationError(
            "Total penjualan tidak sesuai harga dan jumlah pesanan."
        )

    nomor_jurnal = f"PJ-ORDER-{order.pk}"

    if JournalEntry.objects.filter(
        entry_number=nomor_jurnal
    ).exists():
        raise ValidationError(
            "Jurnal untuk pesanan ini sudah tercatat."
        )

    if InventoryMovement.objects.filter(
        reference=nomor_jurnal
    ).exists():
        raise ValidationError(
            "Mutasi stok untuk pesanan ini sudah tercatat."
        )

    # Akun debit mengikuti metode pembayaran.
    kode_debit = (
        "1010" if order.payment_method == "cash" else "1030"
    )

    akun_debit = Account.objects.get(
        account_code=kode_debit,
        is_active=True,
    )
    akun_penjualan = Account.objects.get(
        account_code="4010",
        is_active=True,
    )

    # Buat mutasi stok keluar.
    mutasi = InventoryMovement.objects.create(
        item=item,
        movement_date=tanggal_transaksi,
        movement_type=InventoryMovement.MovementType.OUT,
        quantity=jumlah,
        unit_cost=Decimal("0.00"),
        reference=nomor_jurnal,
        description=f"Penjualan pesanan #{order.pk}",
    )

    # Buat jurnal dalam status Draft terlebih dahulu.
    jurnal = JournalEntry.objects.create(
        entry_number=nomor_jurnal,
        accounting_period=periode,
        entry_date=tanggal_transaksi,
        entry_type=JournalEntry.EntryType.GENERAL,
        description=f"Penjualan pesanan #{order.pk}",
        status=JournalEntry.Status.DRAFT,
    )

    JournalLine.objects.create(
        journal_entry=jurnal,
        account=akun_debit,
        debit=nilai_penjualan,
        credit=Decimal("0.00"),
    )

    JournalLine.objects.create(
        journal_entry=jurnal,
        account=akun_penjualan,
        debit=Decimal("0.00"),
        credit=nilai_penjualan,
    )

    # Posting setelah dua baris jurnal lengkap.
    jurnal.status = JournalEntry.Status.POSTED
    jurnal.save()

    # Tandai bahwa order sudah diproses.
    order.stock_out_movement = mutasi
    order.sales_journal = jurnal
    order.status = "selesai"
    order.save(
        update_fields=[
            "stock_out_movement",
            "sales_journal",
            "status",
        ]
    )

    return order
