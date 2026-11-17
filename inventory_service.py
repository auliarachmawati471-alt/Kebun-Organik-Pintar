
from decimal import Decimal

from django.db.models import Sum

from core.models import InventoryMovement


def hitung_stok(item):
    """
    Menghitung stok berdasarkan seluruh mutasi
    yang sudah tersimpan dalam database.
    """
    nol = Decimal("0.000")

    mutasi = InventoryMovement.objects.filter(item=item)

    total_masuk = (
        mutasi.filter(
            movement_type=InventoryMovement.MovementType.IN
        ).aggregate(total=Sum("quantity"))["total"]
        or nol
    )

    total_keluar = (
        mutasi.filter(
            movement_type=InventoryMovement.MovementType.OUT
        ).aggregate(total=Sum("quantity"))["total"]
        or nol
    )

    return {
        "masuk": total_masuk,
        "keluar": total_keluar,
        "stok_akhir": total_masuk - total_keluar,
    }
