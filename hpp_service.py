
from decimal import Decimal

from django.db.models import Sum

from core.models import JournalEntry, JournalLine
from datetime import timedelta
from core.models import InventoryMovement


def hitung_pembelian_neto(periode):
    nol = Decimal("0.00")

    kode_akun = ["5010", "5020", "5030", "5040"]

    data = (
        JournalLine.objects.filter(
            journal_entry__accounting_period=periode,
            journal_entry__status=JournalEntry.Status.POSTED,
            journal_entry__entry_type__in=[
                JournalEntry.EntryType.GENERAL,
                JournalEntry.EntryType.ADJUSTMENT,
            ],
            account__account_code__in=kode_akun,
        )
        .values("account__account_code")
        .annotate(
            total_debit=Sum("debit"),
            total_kredit=Sum("credit"),
        )
    )

    saldo_akun = {}

    for item in data:
        kode = item["account__account_code"]

        debit = item["total_debit"] or nol
        kredit = item["total_kredit"] or nol

        saldo_akun[kode] = debit - kredit

    pembelian = saldo_akun.get("5010", nol)
    angkut = saldo_akun.get("5020", nol)

    retur = -saldo_akun.get("5030", nol)
    potongan = -saldo_akun.get("5040", nol)

    pembelian_neto = pembelian + angkut - retur - potongan

    return {
        "pembelian": pembelian,
        "angkut": angkut,
        "retur": retur,
        "potongan": potongan,
        "pembelian_neto": pembelian_neto,
    }

def hitung_hpp_fifo_periodik(item, periode):
    """
    Menghitung HPP FIFO periodik dari mutasi persediaan.
    Tidak membuat atau mengubah jurnal.
    """

    def posisi_fifo_sampai(tanggal):
        mutasi = InventoryMovement.objects.filter(
            item=item,
            movement_date__lte=tanggal,
        ).order_by("movement_date", "id")

        lapisan = []
        total_masuk = Decimal("0.000")
        total_keluar = Decimal("0.000")
        nilai_masuk = Decimal("0.00")

        for m in mutasi:
            if m.movement_type == "in":
                if m.quantity <= 0 or m.unit_cost <= 0:
                    raise ValueError(
                        "Jumlah dan biaya stok masuk harus positif."
                    )

                lapisan.append((m.quantity, m.unit_cost))
                total_masuk += m.quantity
                nilai_masuk += m.quantity * m.unit_cost

            elif m.movement_type == "out":
                total_keluar += m.quantity

        if total_keluar > total_masuk:
            raise ValueError(
                "Mutasi keluar melebihi total persediaan."
            )

        stok_akhir = total_masuk - total_keluar
        sisa = stok_akhir
        nilai_akhir = Decimal("0.00")

        # FIFO periodik: stok akhir berasal
        # dari lapisan pembelian/produksi terbaru.
        for jumlah, biaya in reversed(lapisan):
            if sisa == 0:
                break

            diambil = min(sisa, jumlah)
            nilai_akhir += diambil * biaya
            sisa -= diambil

        return {
            "stok_akhir": stok_akhir,
            "nilai_akhir": nilai_akhir,
            "total_keluar": total_keluar,
            "hpp_kumulatif": nilai_masuk - nilai_akhir,
        }

    sebelum = posisi_fifo_sampai(
        periode.start_date - timedelta(days=1)
    )
    akhir = posisi_fifo_sampai(periode.end_date)

    return {
        "jumlah_terjual": (
            akhir["total_keluar"] - sebelum["total_keluar"]
        ),
        "hpp": (
            akhir["hpp_kumulatif"]
            - sebelum["hpp_kumulatif"]
        ),
        "stok_akhir": akhir["stok_akhir"],
        "nilai_persediaan_akhir": akhir["nilai_akhir"],
    }
