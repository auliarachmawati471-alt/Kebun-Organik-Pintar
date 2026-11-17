"""Regression checks for the audit findings; all use Django's isolated test DB."""
import io
import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from django.contrib.auth.models import User, Permission
from django.core.management import call_command, CommandError
from django.core.exceptions import ValidationError
from django.db import connection, IntegrityError, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from .models import Account, AccountingPeriod, Product, Order, JournalEntry, JournalLine, InventoryItem, InventoryMovement, ProductionBatch, ProductionCost
from .services.sales_service import selesaikan_penjualan
from .services.production_service import catat_panen
from .services.financial_report_service import hitung_laba_rugi, hitung_neraca, hitung_nspp


class AuditRegressionTests(TestCase):
    def setUp(self):
        call_command('import_coa',stdout=io.StringIO())
        self.period=AccountingPeriod.objects.create(period_name='November Uji',start_date=date(2026,11,1),end_date=date(2026,11,30))
        self.item=InventoryItem.objects.create(item_code='BAYAM-UJI',item_name='Bayam',item_type='harvest',unit='ikat')
        self.product=Product.objects.create(name='Bayam',price=Decimal('3000.00'),unit='ikat',inventory_item=self.item)
        InventoryMovement.objects.create(item=self.item,movement_date=self.period.start_date,movement_type='in',quantity=10,unit_cost=1000)
        self.journal('OPEN-001',[('1050',10000,0),('3010',0,10000)],kind='opening')

    def journal(self,number,lines,kind='general',when=None):
        j=JournalEntry.objects.create(entry_number=number,accounting_period=self.period,entry_date=when or self.period.start_date,entry_type=kind,description='Transaksi uji')
        for code,debit,credit in lines:
            JournalLine.objects.create(journal_entry=j,account=Account.objects.get(account_code=code),debit=debit,credit=credit)
        j.status='posted';j.save()
        return j

    def order(self):
        return Order.objects.create(customer_name='Pembeli Uji',whatsapp='081234567890',product=self.product,quantity=1,unit_price=3000,total_price=3000,status='diproses',payment_method='cash')

    def staff(self,permissions=()):
        u=User.objects.create_user(username='staf_uji',is_staff=True)
        for code in permissions:
            u.user_permissions.add(Permission.objects.get(codename=code,content_type__app_label='core'))
        self.client.force_login(u)
        return u

    def close(self):
        o=self.order();selesaikan_penjualan(o.pk,self.period.pk,date(2026,11,2))
        self.journal('AJP-HPP',[('5050',1000,0),('1050',0,1000)],kind='adjustment',when=self.period.end_date)
        return self.journal('JP-001',[('4010',3000,0),('5050',0,1000),('3010',0,2000)],kind='closing',when=self.period.end_date)

    def test_anonymous_cannot_read_ledger(self):
        response=self.client.get(reverse('general_ledger'),{'account':Account.objects.get(account_code='1010').pk,'period':self.period.pk})
        self.assertEqual(response.status_code,302)

    def test_staff_without_report_permission_cannot_read_reports(self):
        self.staff()
        for name in ['admin_dashboard','general_ledger','trial_balance','adjusted_trial_balance','worksheet','laporan_laba_rugi','laporan_neraca','laporan_perubahan_ekuitas','neraca_saldo_setelah_penutupan','laporan_jurnal_penutup']:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code,403)

    def test_report_permission_allows_reports(self):
        self.staff(['view_financial_reports'])
        for name in ['admin_dashboard','general_ledger','trial_balance','adjusted_trial_balance','worksheet','laporan_laba_rugi','laporan_neraca','laporan_perubahan_ekuitas','neraca_saldo_setelah_penutupan','laporan_jurnal_penutup']:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code,200)

    def test_view_only_cannot_post_journal(self):
        self.staff(['view_journalentry'])
        j=JournalEntry.objects.create(entry_number='DRAFT-VIEW',accounting_period=self.period,entry_date=self.period.start_date,description='Draft')
        self.client.post(reverse('admin:core_journalentry_changelist'),{'action':'posting_jurnal','_selected_action':str(j.pk),'index':'0'})
        j.refresh_from_db();self.assertEqual(j.status,'draft')

    def test_view_only_cannot_complete_sale(self):
        self.staff(['view_order'])
        order=self.order()
        self.client.post(reverse('admin:core_order_changelist'),{'action':'proses_penjualan','_selected_action':str(order.pk),'index':'0','periode_jual':self.period.pk,'tanggal_jual':'2026-11-02'})
        order.refresh_from_db();self.assertEqual(order.status,'diproses');self.assertIsNone(order.sales_journal_id)

    def test_view_only_cannot_harvest(self):
        self.staff(['view_productionbatch'])
        b=ProductionBatch.objects.create(batch_code='B-VIEW',planting_date=self.period.start_date)
        self.client.post(reverse('admin:core_productionbatch_changelist'),{'action':'proses_panen_terpilih','_selected_action':str(b.pk),'index':'0','konfirmasi_panen':'1','item':self.item.pk,'tanggal_panen':'2026-11-02','jumlah_panen':'1','biaya_per_unit':'1000'})
        b.refresh_from_db();self.assertEqual(b.status,'growing');self.assertIsNone(b.harvest_movement_id)

    def test_valid_closing_locks_period_and_leaves_permanent_accounts(self):
        self.close();self.period.refresh_from_db()
        self.assertEqual(self.period.status,'closed')
        report=hitung_nspp(self.period)
        self.assertTrue(report['is_final']);self.assertEqual(report['selisih'],0)
        self.assertFalse(any(r['kode'].startswith(('4','5','6')) for r in report['daftar_akun']))

    def test_posting_and_duplicate_closing_after_close_are_rejected(self):
        self.close();self.period.refresh_from_db()
        for kind in ['general','adjustment','closing']:
            with self.subTest(kind=kind),self.assertRaises(ValidationError):
                JournalEntry.objects.create(entry_number='AFTER-'+kind,accounting_period=self.period,entry_date=self.period.end_date,entry_type=kind,description='Tidak sah')

    def test_closing_rejects_missing_hpp_adjustment(self):
        o=self.order();selesaikan_penjualan(o.pk,self.period.pk,date(2026,11,2))
        with self.assertRaises(ValidationError):
            self.journal('JP-BAD',[('4010',3000,0),('3010',0,3000)],kind='closing',when=self.period.end_date)
        self.period.refresh_from_db();self.assertEqual(self.period.status,'open')

    def test_opening_does_not_depend_on_SA_number(self):
        result=hitung_neraca(self.period)
        self.assertEqual(result['total_aset'],10000);self.assertEqual(result['total_ekuitas'],10000);self.assertEqual(result['selisih'],0)

    def test_additional_expense_account_is_included(self):
        Account.objects.create(account_code='6990',account_name='Beban Tambahan',category='expense',normal_balance='debit')
        self.journal('BEBAN',[('6990',100,0),('1010',0,100)])
        self.assertEqual(hitung_laba_rugi(self.period)['laba_bersih'],-100)
        self.assertEqual(hitung_neraca(self.period)['selisih'],0)

    def test_posted_journal_and_lines_cannot_be_deleted_or_bulk_updated(self):
        j=self.journal('LOCK',[('1010',100,0),('3010',0,100)])
        for action in [lambda:j.delete(),lambda:JournalEntry.objects.filter(pk=j.pk).delete(),lambda:j.lines.all().delete(),lambda:j.lines.all().update(debit=999)]:
            with self.assertRaises(ValidationError):action()
        self.assertEqual(j.lines.count(),2)

    def test_invalid_bulk_line_rejected_and_sql_constraint_exists(self):
        j=JournalEntry.objects.create(entry_number='DRAFT',accounting_period=self.period,entry_date=self.period.start_date,description='Draft')
        with self.assertRaises(ValidationError):
            JournalLine.objects.bulk_create([JournalLine(journal_entry=j,account=Account.objects.get(account_code='1010'),debit=-100,credit=50)])
        with self.assertRaises(IntegrityError),transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('INSERT INTO core_journalline (journal_entry_id,account_id,description,debit,credit) VALUES (%s,%s,%s,%s,%s)',[j.pk,Account.objects.get(account_code='1010').pk,'Tidak sah',-1,0])

    def test_completed_order_cannot_change_or_be_deleted(self):
        o=self.order();selesaikan_penjualan(o.pk,self.period.pk,date(2026,11,2));o.refresh_from_db()
        o.quantity=2;o.total_price=6000
        with self.assertRaises(ValidationError):o.save()
        with self.assertRaises(ValidationError):Order.objects.filter(pk=o.pk).delete()
        with self.assertRaises(ValidationError):Order.objects.filter(pk=o.pk).update(quantity=2,total_price=6000)

    def test_inventory_cannot_be_bulk_deleted_or_backdated(self):
        with self.assertRaises(ValidationError):InventoryMovement.objects.all().delete()
        with self.assertRaises(ValidationError):InventoryMovement.objects.create(item=self.item,movement_date=date(2026,10,31),movement_type='out',quantity=1)

    def test_zero_incoming_cost_is_rejected(self):
        with self.assertRaises(ValidationError):InventoryMovement.objects.create(item=self.item,movement_date=self.period.start_date,movement_type='in',quantity=1,unit_cost=0)

    def test_invalid_or_overlapping_period_is_rejected(self):
        with self.assertRaises(ValidationError):AccountingPeriod.objects.create(period_name='Terbalik',start_date=date(2027,2,1),end_date=date(2027,1,1))
        with self.assertRaises(ValidationError):AccountingPeriod.objects.create(period_name='Tumpang tindih',start_date=date(2026,11,5),end_date=date(2026,12,5))

    def test_production_cost_cannot_exceed_source(self):
        b=ProductionBatch.objects.create(batch_code='B-COST',planting_date=self.period.start_date)
        j=self.journal('WIP',[('1080',100,0),('1010',0,100)])
        line=j.lines.get(debit__gt=0)
        ProductionCost.objects.create(batch=b,cost_date=b.planting_date,description='Biaya satu',amount=100,journal_line=line)
        with self.assertRaises(ValidationError):ProductionCost.objects.create(batch=b,cost_date=b.planting_date,description='Biaya ganda',amount=100,journal_line=line)

    def test_harvest_requires_cost_and_creates_matching_transfer_journal(self):
        b=ProductionBatch.objects.create(batch_code='B-PANEN',planting_date=self.period.start_date)
        with self.assertRaises(ValidationError):catat_panen(b.pk,self.item.pk,date(2026,11,2),2,500)
        j=self.journal('WIP',[('1080',1000,0),('1010',0,1000)])
        ProductionCost.objects.create(batch=b,cost_date=b.planting_date,description='Biaya',amount=1000,journal_line=j.lines.get(debit__gt=0))
        with self.assertRaises(ValidationError):catat_panen(b.pk,self.item.pk,date(2026,11,2),2,999)
        catat_panen(b.pk,self.item.pk,date(2026,11,2),2,500)
        b.refresh_from_db();self.assertIsNotNone(b.harvest_journal_id)
        self.assertTrue(b.harvest_journal.lines.filter(account__account_code='1050',debit=1000).exists())
        self.assertTrue(b.harvest_journal.lines.filter(account__account_code='1080',credit=1000).exists())

    def test_checkout_replay_redirects_to_same_order(self):
        response=self.client.get(reverse('checkout',args=[self.product.pk]))
        token=response.context['form'].initial['checkout_token']
        data={'customer_name':'Pembeli Uji','whatsapp':'081234567890','quantity':'1','checkout_token':token}
        a=self.client.post(reverse('checkout',args=[self.product.pk]),data)
        b=self.client.post(reverse('checkout',args=[self.product.pk]),data)
        self.assertEqual(a.status_code,302);self.assertEqual(a['Location'],b['Location']);self.assertEqual(Order.objects.count(),1)
        self.assertEqual(self.client.get(a['Location']).status_code,200)

    def test_checkout_rejects_bad_contact_overlength_and_tampered_token(self):
        response=self.client.get(reverse('checkout',args=[self.product.pk]))
        token=response.context['form'].initial['checkout_token']
        for fields in [dict(customer_name='X'*101,whatsapp='081234567890',checkout_token=token),dict(customer_name='Uji',whatsapp='bukan nomor',checkout_token=token),dict(customer_name='Uji',whatsapp='081234567890',checkout_token='rusak')]:
            result=self.client.post(reverse('checkout',args=[self.product.pk]),dict(fields,quantity='1'))
            self.assertEqual(result.status_code,200);self.assertTrue(result.context['form'].errors)
        self.assertEqual(Order.objects.count(),0)

    def test_malformed_filters_return_404(self):
        self.staff(['view_financial_reports'])
        for name,key in [('general_ledger','account'),('trial_balance','period'),('laporan_laba_rugi','periode')]:
            with self.subTest(name=name):self.assertEqual(self.client.get(reverse(name),{key:'abc'}).status_code,404)

    def test_safe_coa_import_does_not_overwrite_used_accounts(self):
        account=Account.objects.get(account_code='1050')
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'old.json'
            source.write_text(json.dumps([dict(account_code='1050',account_name='Perlengkapan Lama',category='asset',normal_balance='debit')]),encoding='utf-8')
            with self.assertRaises(CommandError):call_command('import_coa',source=str(source),stdout=io.StringIO())
        account.refresh_from_db();self.assertEqual(account.account_name,'Persediaan Bayam Hasil Panen')

    def test_admin_invalid_sale_returns_message_instead_of_500(self):
        u=User.objects.create_superuser(username='pengelola');self.client.force_login(u)
        o=self.order();o.status='menunggu';o.save()
        result=self.client.post(reverse('admin:core_order_changelist'),{'action':'proses_penjualan','_selected_action':str(o.pk),'index':'0','periode_jual':self.period.pk,'tanggal_jual':'2026-11-02'})
        self.assertEqual(result.status_code,302);o.refresh_from_db();self.assertIsNone(o.sales_journal_id)

    def test_trial_balance_uses_grouped_query(self):
        self.staff(['view_financial_reports'])
        with CaptureQueriesContext(connection) as queries:
            result=self.client.get(reverse('trial_balance'))
        self.assertEqual(result.status_code,200)
        self.assertLess(len(queries),15)
