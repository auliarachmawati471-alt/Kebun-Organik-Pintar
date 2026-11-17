
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from core.models import (
    Account, AccountingPeriod, JournalEntry, JournalLine,
    InventoryItem,
    InventoryMovement,
    ProductionBatch,
    ProductionCost,
)


@transaction.atomic
def catat_panen(
    batch_id,
    item_id,
    tanggal_panen,
    jumlah_panen,
    biaya_per_unit,
):
    # 1. Ambil kelompok tanam dan barang hasil panen.
    batch = ProductionBatch.objects.select_for_update().get(
        pk=batch_id
    )
    item = InventoryItem.objects.get(pk=item_id)

    # 2. Cegah pencatatan panen kedua.
    if (
        batch.status != ProductionBatch.Status.GROWING
        or batch.harvest_movement_id is not None
    ):
        raise ValidationError(
            "Kelompok tanam ini tidak dapat dipanen lagi."
        )

    # 3. Pastikan barang adalah hasil panen.
    if item.item_type != InventoryItem.ItemType.HARVEST:
        raise ValidationError(
            "Barang harus berjenis Hasil Panen."
        )

    if not item.is_active:
        raise ValidationError(
            "Barang hasil panen harus aktif."
        )

    # 4. Validasi tanggal.
    if not isinstance(tanggal_panen, date):
        raise ValidationError("Tanggal panen tidak valid.")

    if tanggal_panen < batch.planting_date:
        raise ValidationError(
            "Tanggal panen tidak boleh sebelum tanggal tanam."
        )

    # 5. Validasi jumlah dan biaya.
    try:
        jumlah = Decimal(str(jumlah_panen))
        biaya = Decimal(str(biaya_per_unit))
    except (InvalidOperation, ValueError):
        raise ValidationError(
            "Jumlah atau biaya per unit tidak valid."
        )

    if not jumlah.is_finite() or jumlah <= 0:
        raise ValidationError(
            "Jumlah hasil panen harus lebih dari nol."
        )

    if not biaya.is_finite() or biaya <= 0:
        raise ValidationError(
            "Biaya per unit harus lebih dari nol."
        )

    # Panen memakai biaya batch yang sudah dibuktikan oleh debit WIP.
    costs = batch.costs.select_related('journal_line__journal_entry','journal_line__account')
    if not costs.exists() or costs.filter(journal_line__isnull=True).exists():
        raise ValidationError('Lengkapi biaya produksi dan hubungkan semua biaya ke jurnal WIP Posted sebelum panen.')
    total = costs.aggregate(value=Sum('amount'))['value'] or Decimal('0')
    if (jumlah*biaya).quantize(Decimal('0.01')) != total.quantize(Decimal('0.01')):
        raise ValidationError('Jumlah panen dikali biaya per unit harus sama dengan total biaya batch.')
    periods = AccountingPeriod.objects.filter(is_legacy=False,status='open',start_date__lte=tanggal_panen,end_date__gte=tanggal_panen)
    if periods.count()!=1:
        raise ValidationError('Pilih tanggal panen pada satu periode aktif yang terbuka.')
    period=periods.get()
    wip=Account.objects.get(account_code='1080',is_active=True)
    finished=Account.objects.get(account_code='1050',is_active=True)
    for cost in costs:
        line=cost.journal_line
        if line.journal_entry.status!='posted' or line.account_id!=wip.pk or line.debit<=0:
            raise ValidationError('Sumber biaya produksi harus debit WIP Posted.')
        allocated=ProductionCost.objects.filter(journal_line=line).aggregate(value=Sum('amount'))['value'] or Decimal('0')
        if allocated>line.debit:
            raise ValidationError('Alokasi biaya produksi melebihi jurnal sumber.')
    journal=JournalEntry.objects.create(entry_number=f'PRD-BATCH-{batch.pk}',accounting_period=period,entry_date=tanggal_panen,description=f'Transfer biaya panen {batch.batch_code}')
    JournalLine.objects.create(journal_entry=journal,account=finished,debit=total)
    JournalLine.objects.create(journal_entry=journal,account=wip,credit=total)
    journal.status='posted'
    journal.save()

    # 6. Catat hasil panen sebagai persediaan masuk.
    mutasi = InventoryMovement.objects.create(
        item=item,
        movement_date=tanggal_panen,
        movement_type=InventoryMovement.MovementType.IN,
        quantity=jumlah,
        unit_cost=biaya,
        reference=f"PANEN-{batch.batch_code}",
        description=f"Hasil panen {batch.batch_code}",
    )

    # 7. Validasi status baru sebelum menyimpan.
    batch.harvest_date = tanggal_panen
    batch.status = ProductionBatch.Status.HARVESTED
    batch.harvest_movement = mutasi
    batch.full_clean()

    batch.harvest_journal = journal
    batch.save(update_fields=['harvest_date','status','harvest_movement','harvest_journal'])

    batch.refresh_from_db()
    return batch


def hitung_biaya_produksi(batch_id):
    """
    Menghitung total biaya yang dicatat
    untuk satu kelompok tanam.
    """

    biaya = ProductionCost.objects.filter(
        batch_id=batch_id
    )

    total = biaya.aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0.00")

    belum_terhubung_jurnal = biaya.filter(
        journal_line__isnull=True
    ).count()

    return {
        "total_biaya": total,
        "jumlah_komponen": biaya.count(),
        "belum_terhubung_jurnal": belum_terhubung_jurnal,
    }
