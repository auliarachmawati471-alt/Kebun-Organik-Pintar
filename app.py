"""Antarmuka Streamlit untuk Kebun Pintar SIA.
Backend model, validasi, database dan service bisnis Django dipertahankan agar
aturan transaksi yang sudah ada tidak hilang saat antarmuka diganti.
"""
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.local')
import django
django.setup()

import streamlit as st
import pandas as pd
from datetime import date
from decimal import Decimal
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.conf import settings
from core.models import (Product, Order, Account, AccountingPeriod, JournalEntry,
    JournalLine, InventoryItem, InventoryMovement, ProductionBatch, ProductionCost)
from core.services.inventory_service import hitung_stok
from core.services.sales_service import selesaikan_penjualan
from core.services.production_service import catat_panen, hitung_biaya_produksi
from core.services.financial_report_service import (hitung_laba_rugi,
    hitung_perubahan_ekuitas, hitung_neraca, hitung_nspp, inventory_reconciliation)

st.set_page_config(page_title='Kebun Pintar SIA', page_icon='🌱', layout='wide')

st.title("🌱 Kebun Organik Pintar")

# Helpers
def rupiah(v):
    try: return 'Rp ' + f'{Decimal(str(v)):,.0f}'.replace(',', '.')
    except Exception: return str(v)
def df_records(qs, fields):
    return pd.DataFrame(list(qs.values(*fields)))
def notify_error(e): st.error(str(e))
def active_periods(): return AccountingPeriod.objects.filter(status='open',is_legacy=False)
def period_select(label='Periode'):
    periods=list(AccountingPeriod.objects.all())
    if not periods: st.warning('Belum ada periode akuntansi.'); return None
    chosen=st.selectbox(label,periods,format_func=lambda p:f'{p.period_name} ({p.status})')
    return chosen

def require_staff():
    u=st.session_state.get('user')
    if not u or not u.is_authenticated or not u.is_staff:
        st.warning('Silakan masuk menggunakan akun staf/admin untuk membuka menu pengelolaan.')
        return False
    return True

def login_box():
    with st.sidebar.expander('Login Admin / Staf', expanded=not bool(st.session_state.get('user'))):
        if st.session_state.get('user'):
            st.write(f"Masuk sebagai **{st.session_state.user.username}**")
            if st.button('Keluar'): st.session_state.pop('user',None); st.rerun()
        else:
            with st.form('login'):
                username=st.text_input('Username'); password=st.text_input('Password',type='password')
                ok=st.form_submit_button('Masuk')
            if ok:
                user=authenticate(username=username,password=password)
                if user and user.is_active and user.is_staff:
                    st.session_state.user=user; st.success('Login berhasil.'); st.rerun()
                else: st.error('Username/password salah atau akun bukan staf aktif.')

login_box()
st.sidebar.markdown('## 🌱 Kebun Pintar SIA')
menu=st.sidebar.radio('Navigasi',[
    'Beranda & Pemesanan','Dashboard Admin','Produk','Pesanan','Daftar Akun','Periode Akuntansi',
    'Jurnal Umum','Buku Besar','Neraca Saldo','Neraca Saldo Setelah Penyesuaian',
    'Neraca Lajur','Laporan Laba Rugi','Perubahan Ekuitas','Neraca','Jurnal Penutup & NS Setelah Penutupan',
    'Persediaan','Produksi & Panen'])
st.sidebar.caption('Sistem Informasi Akuntansi Kebun Pintar')

if menu=='Beranda & Pemesanan':
    st.write('Pilih produk sayuran segar dan lakukan pemesanan.')
    products=list(Product.objects.filter(is_active=True).select_related('inventory_item'))
    if not products: st.info('Belum ada produk aktif.')
    cols=st.columns(3)
    for i,p in enumerate(products):
        with cols[i%3]:
            if p.image:
                try: st.image(p.image.url,use_container_width=True)
                except Exception: pass
            st.subheader(p.name); st.write(p.description or 'Sayuran segar dari Kebun Pintar')
            st.write(f'**{rupiah(p.price) if p.price else "Harga belum tersedia"} / {p.unit}**')
            try: stock=p.stok_tersedia
            except Exception: stock=0
            st.caption(f'Stok tersedia: {stock} {p.unit}')
            with st.form(f'order_{p.pk}'):
                name=st.text_input('Nama Pembeli',key=f'n_{p.pk}')
                wa=st.text_input('Nomor WhatsApp',key=f'w_{p.pk}',placeholder='08xxxxxxxxxx')
                qty=st.number_input('Jumlah Pesanan',1,999999,1,key=f'q_{p.pk}')
                submit=st.form_submit_button('Pesan Sekarang',disabled=not p.price or not p.inventory_item_id or stock<1)
            if submit:
                try:
                    wa_clean=wa.strip()
                    if not name.strip(): raise ValueError('Nama pembeli wajib diisi.')
                    if not __import__('re').fullmatch(r'\+?\d{8,15}',wa_clean): raise ValueError('Nomor WhatsApp harus 8–15 digit, boleh diawali +.')
                    if qty>stock: raise ValueError(f'Stok hanya {stock} {p.unit}.')
                    with transaction.atomic():
                        order=Order.objects.create(customer_name=name.strip(),whatsapp=wa_clean,product=p,quantity=qty,unit_price=p.price,total_price=p.price*qty)
                    st.success(f'Pesanan #{order.pk} berhasil dibuat. Total {rupiah(order.total_price)}. Status: Menunggu Konfirmasi.')
                    wa_target=getattr(settings,'WHATSAPP_CONTACT','')
                    if wa_target:
                        msg=f"Halo, saya {name}. Saya memesan {p.name} sebanyak {qty} {p.unit}. Total {rupiah(order.total_price)}. Nomor pesanan #{order.pk}."
                        import urllib.parse
                        st.markdown(f'[Konfirmasi melalui WhatsApp](https://wa.me/{wa_target}?text={urllib.parse.quote(msg)})')
                except Exception as e: notify_error(e)

elif menu=='Dashboard Admin':
    if require_staff():
        st.title('Dashboard Admin')
        total=Order.objects.count(); waiting=Order.objects.filter(status='menunggu').count(); done=Order.objects.filter(status='selesai').count()
        c1,c2,c3=st.columns(3); c1.metric('Total Pesanan',total); c2.metric('Menunggu Konfirmasi',waiting); c3.metric('Pesanan Selesai',done)
        st.subheader('Pesanan Terbaru')
        st.dataframe(df_records(Order.objects.select_related('product').order_by('-created_at')[:30],['id','customer_name','whatsapp','product__name','quantity','total_price','status','created_at']),use_container_width=True,hide_index=True)
        st.subheader('Produk Aktif'); st.dataframe(df_records(Product.objects.filter(is_active=True),['id','name','price','unit','is_active']),use_container_width=True,hide_index=True)

elif menu=='Produk':
    if require_staff():
        st.title('Pengelolaan Produk')
        with st.form('new_product'):
            name=st.text_input('Nama Produk'); desc=st.text_area('Deskripsi'); price=st.number_input('Harga (Rp)',min_value=0,step=500); unit=st.text_input('Satuan',value='ikat')
            items=list(InventoryItem.objects.filter(item_type='harvest',is_active=True))
            item=st.selectbox('Hubungkan ke Barang Hasil Panen',items,format_func=str,index=None)
            active=st.checkbox('Produk Aktif',True); save=st.form_submit_button('Tambah Produk')
        if save:
            try: Product.objects.create(name=name,description=desc,price=Decimal(price) if price else None,unit=unit,inventory_item=item,is_active=active); st.success('Produk ditambahkan.'); st.rerun()
            except Exception as e: notify_error(e)
        st.dataframe(df_records(Product.objects.select_related('inventory_item'),['id','name','description','price','unit','stock','inventory_item__item_name','is_active']),use_container_width=True,hide_index=True)
        ids=list(Product.objects.all())
        if ids:
            p=st.selectbox('Pilih produk untuk ubah status',ids,format_func=lambda x:f'{x.pk} - {x.name}')
            new_active=st.toggle('Produk aktif',value=p.is_active,key='toggle_product')
            if st.button('Simpan status produk'):
                try: p.is_active=new_active; p.save(); st.success('Status disimpan.'); st.rerun()
                except Exception as e: notify_error(e)

elif menu=='Pesanan':
    if require_staff():
        st.title('Pengelolaan Pesanan')
        st.dataframe(df_records(Order.objects.select_related('product').order_by('-created_at'),['id','customer_name','whatsapp','product__name','quantity','unit_price','total_price','status','payment_method','requires_reconciliation','created_at']),use_container_width=True,hide_index=True)
        orders=list(Order.objects.exclude(status__in=['selesai','dibatalkan']))
        if orders:
            o=st.selectbox('Pilih Pesanan',orders,format_func=lambda x:f'#{x.pk} - {x.customer_name} ({x.status})')
            status=st.selectbox('Ubah Status', ['menunggu','dikonfirmasi','diproses','dibatalkan'],index=['menunggu','dikonfirmasi','diproses','dibatalkan'].index(o.status) if o.status in ['menunggu','dikonfirmasi','diproses','dibatalkan'] else 0)
            if st.button('Simpan Status'):
                try: o.status=status; o.save(); st.success('Status diperbarui.'); st.rerun()
                except Exception as e: notify_error(e)
            if o.status in ['dikonfirmasi','diproses']:
                periods=list(active_periods())
                with st.form('finish_sale'):
                    period=st.selectbox('Periode Akuntansi',periods,format_func=str,index=None)
                    trxdate=st.date_input('Tanggal Transaksi',value=date.today())
                    payment=st.selectbox('Metode Pembayaran',['cash','credit'],format_func=lambda x:'Tunai' if x=='cash' else 'Kredit')
                    finish=st.form_submit_button('Selesaikan Penjualan & Buat Jurnal')
                if finish:
                    try:
                        o.payment_method=payment; o.save(); selesaikan_penjualan(o.pk,period.pk,trxdate); st.success('Penjualan selesai, stok keluar dan jurnal penjualan Posted dibuat.'); st.rerun()
                    except Exception as e: notify_error(e)

elif menu=='Daftar Akun':
    if require_staff():
        st.title('Daftar Akun (Chart of Accounts)')
        with st.form('account_add'):
            code=st.text_input('Kode Akun'); nm=st.text_input('Nama Akun'); cat=st.selectbox('Kategori',['asset','liability','equity','revenue','expense']); normal=st.selectbox('Saldo Normal',['debit','credit']); save=st.form_submit_button('Tambah Akun')
        if save:
            try: Account.objects.create(account_code=code,account_name=nm,category=cat,normal_balance=normal); st.success('Akun ditambahkan.'); st.rerun()
            except Exception as e: notify_error(e)
        st.dataframe(df_records(Account.objects.all(),['id','account_code','account_name','category','normal_balance','is_active']),use_container_width=True,hide_index=True)

elif menu=='Periode Akuntansi':
    if require_staff():
        st.title('Periode Akuntansi')
        with st.form('period_add'):
            nm=st.text_input('Nama Periode'); start=st.date_input('Tanggal Mulai'); end=st.date_input('Tanggal Selesai'); save=st.form_submit_button('Buat Periode')
        if save:
            try: AccountingPeriod.objects.create(period_name=nm,start_date=start,end_date=end); st.success('Periode dibuat.'); st.rerun()
            except Exception as e: notify_error(e)
        st.dataframe(df_records(AccountingPeriod.objects.all(),['id','period_name','start_date','end_date','status','is_legacy']),use_container_width=True,hide_index=True)

elif menu=='Jurnal Umum':
    if require_staff():
        st.title('Jurnal Umum')
        with st.form('journal_create'):
            periods=list(active_periods()); p=st.selectbox('Periode',periods,format_func=str,index=None)
            no=st.text_input('Nomor Jurnal'); d=st.date_input('Tanggal'); desc=st.text_input('Keterangan'); typ=st.selectbox('Jenis',['general','opening','adjustment'])
            accounts=list(Account.objects.filter(is_active=True)); st.caption('Isi minimal dua baris jurnal. Debit dan kredit harus seimbang.')
            lines=[]
            for i in range(4):
                a=st.selectbox(f'Akun baris {i+1}',[None]+accounts,format_func=lambda x:'-- pilih akun --' if x is None else str(x),key=f'ja{i}')
                c1,c2=st.columns(2); debit=c1.number_input(f'Debit {i+1}',min_value=0.0,step=1000.0,key=f'jd{i}'); credit=c2.number_input(f'Kredit {i+1}',min_value=0.0,step=1000.0,key=f'jk{i}')
                lines.append((a,Decimal(str(debit)),Decimal(str(credit))))
            save=st.form_submit_button('Simpan Jurnal sebagai Draft')
        if save:
            try:
                with transaction.atomic():
                    j=JournalEntry.objects.create(entry_number=no,accounting_period=p,entry_date=d,description=desc,entry_type=typ,status='draft')
                    for a,db,cr in lines:
                        if a and (db or cr): JournalLine.objects.create(journal_entry=j,account=a,debit=db,credit=cr)
                st.success(f'Jurnal {j.entry_number} tersimpan sebagai Draft.'); st.rerun()
            except Exception as e: notify_error(e)
        drafts=list(JournalEntry.objects.filter(status='draft').select_related('accounting_period'))
        if drafts:
            j=st.selectbox('Draft untuk diposting',drafts,format_func=lambda x:f'{x.entry_number} - {x.description}')
            st.dataframe(df_records(j.lines.select_related('account'),['account__account_code','account__account_name','debit','credit']),use_container_width=True,hide_index=True)
            if st.button('Posting Jurnal Terpilih'):
                try: j.status='posted'; j.save(); st.success('Jurnal berhasil Posted.'); st.rerun()
                except Exception as e: notify_error(e)
        st.subheader('Riwayat Jurnal'); st.dataframe(df_records(JournalEntry.objects.select_related('accounting_period'),['entry_number','entry_date','accounting_period__period_name','description','entry_type','status']),use_container_width=True,hide_index=True)

elif menu in ['Buku Besar','Neraca Saldo','Neraca Saldo Setelah Penyesuaian','Neraca Lajur','Laporan Laba Rugi','Perubahan Ekuitas','Neraca','Jurnal Penutup & NS Setelah Penutupan']:
    if require_staff():
        st.title(menu); p=period_select()
        if p:
            if not st.session_state.user.has_perm('core.view_financial_reports'):
                st.error('Akun ini belum memiliki izin Dapat melihat laporan keuangan.')
            elif menu=='Buku Besar':
                accounts=list(Account.objects.all()); a=st.selectbox('Pilih Akun',accounts,format_func=str)
                lines=JournalLine.objects.filter(account=a,journal_entry__accounting_period=p,journal_entry__status='posted').select_related('journal_entry').order_by('journal_entry__entry_date','id')
                rows=[]; bal=Decimal('0')
                for l in lines:
                    bal+=l.debit-l.credit; rows.append({'Tanggal':l.journal_entry.entry_date,'No Jurnal':l.journal_entry.entry_number,'Keterangan':l.journal_entry.description,'Debit':l.debit,'Kredit':l.credit,'Saldo':bal})
                st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
            elif menu in ['Neraca Saldo','Neraca Saldo Setelah Penyesuaian','Neraca Lajur']:
                kinds=['opening','general'] if menu=='Neraca Saldo' else ['opening','general','adjustment']
                qs=JournalLine.objects.filter(journal_entry__accounting_period=p,journal_entry__status='posted',journal_entry__entry_type__in=kinds).values('account__account_code','account__account_name').annotate(debit=Sum('debit'),credit=Sum('credit')).order_by('account__account_code')
                data=[]
                for x in qs:
                    saldo=Decimal(x['debit'] or 0)-Decimal(x['credit'] or 0)
                    data.append({'Kode':x['account__account_code'],'Nama Akun':x['account__account_name'],'Debit':max(saldo,0),'Kredit':max(-saldo,0)})
                st.dataframe(pd.DataFrame(data),use_container_width=True,hide_index=True)
                st.metric('Total Debit',rupiah(sum((r['Debit'] for r in data),Decimal(0)))); st.metric('Total Kredit',rupiah(sum((r['Kredit'] for r in data),Decimal(0))))
                if menu=='Neraca Lajur': st.caption('Kolom penyesuaian dan saldo setelah penyesuaian ditampilkan melalui laporan neraca saldo setelah penyesuaian; data bersumber dari jurnal Posted.')
            elif menu=='Laporan Laba Rugi':
                r=hitung_laba_rugi(p); st.metric('Penjualan Bersih',rupiah(r['penjualan_bersih'])); st.metric('HPP',rupiah(r['hpp'])); st.metric('Laba Kotor',rupiah(r['laba_kotor'])); st.subheader('Beban'); st.dataframe(pd.DataFrame([{'Nama Beban':k,'Jumlah':v} for k,v in r['beban'].items()]),use_container_width=True,hide_index=True); st.metric('Total Beban',rupiah(r['total_beban'])); st.metric('Laba / Rugi Bersih',rupiah(r['laba_bersih']))
                rec=inventory_reconciliation(p); st.caption(f"Rekonsiliasi FIFO — HPP: {rupiah(rec['fifo_hpp'])}; Persediaan akhir: {rupiah(rec['fifo_stock'])}")
                for w in rec['warnings']: st.warning(w)
            elif menu=='Perubahan Ekuitas':
                r=hitung_perubahan_ekuitas(p); st.dataframe(pd.DataFrame([{'Komponen':k.replace('_',' ').title(),'Jumlah':v} for k,v in r.items()]),use_container_width=True,hide_index=True); st.metric('Ekuitas Akhir',rupiah(r['ekuitas_akhir']))
            elif menu=='Neraca':
                r=hitung_neraca(p); st.subheader('Aset Lancar'); st.dataframe(pd.DataFrame([{'Akun':k,'Saldo':v} for k,v in r['aset_lancar'].items()]),use_container_width=True,hide_index=True); st.subheader('Aset Tetap'); st.dataframe(pd.DataFrame([{'Akun':k,'Saldo':v} for k,v in r['aset_tetap'].items()]),use_container_width=True,hide_index=True); st.subheader('Kewajiban'); st.dataframe(pd.DataFrame([{'Akun':k,'Saldo':v} for k,v in r['kewajiban'].items()]),use_container_width=True,hide_index=True); c1,c2,c3=st.columns(3); c1.metric('Total Aset',rupiah(r['total_aset'])); c2.metric('Total Kewajiban + Ekuitas',rupiah(r['total_kewajiban_ekuitas'])); c3.metric('Selisih',rupiah(r['selisih']))
            elif menu=='Jurnal Penutup & NS Setelah Penutupan':
                st.subheader('Jurnal Penutup'); st.dataframe(df_records(JournalEntry.objects.filter(accounting_period=p,entry_type='closing').order_by('-entry_date'),['entry_number','entry_date','description','status']),use_container_width=True,hide_index=True)
                r=hitung_nspp(p); st.subheader('Neraca Saldo Setelah Penutupan'); st.dataframe(pd.DataFrame(r['daftar_akun']),use_container_width=True,hide_index=True); st.metric('Selisih Debit-Kredit',rupiah(r['selisih']))
                for w in r['warnings']: st.warning(w)
                st.subheader('Buat Jurnal Penutup')
                with st.form('closing_journal_form'):
                    cno=st.text_input('Nomor Jurnal Penutup',value=f'JP-{p.period_name}')
                    cdesc=st.text_input('Keterangan Penutup',value=f'Jurnal penutup periode {p.period_name}')
                    caccounts=list(Account.objects.filter(is_active=True))
                    clines=[]
                    for i in range(6):
                        ca=st.selectbox(f'Akun penutup baris {i+1}',[None]+caccounts,format_func=lambda x:'-- pilih akun --' if x is None else str(x),key=f'ca{i}')
                        x1,x2=st.columns(2); cd=x1.number_input(f'Debit penutup {i+1}',min_value=0.0,step=1000.0,key=f'cd{i}'); cc=x2.number_input(f'Kredit penutup {i+1}',min_value=0.0,step=1000.0,key=f'cc{i}')
                        clines.append((ca,Decimal(str(cd)),Decimal(str(cc))))
                    close=st.form_submit_button('Simpan Draft dan Posting Jurnal Penutup')
                if close:
                    try:
                        with transaction.atomic():
                            cj=JournalEntry.objects.create(entry_number=cno,accounting_period=p,entry_date=p.end_date,description=cdesc,entry_type='closing',status='draft')
                            for ca,cd,cc in clines:
                                if ca and (cd or cc): JournalLine.objects.create(journal_entry=cj,account=ca,debit=cd,credit=cc)
                            cj.status='posted'; cj.save()
                        st.success('Jurnal penutup berhasil diposting dan periode ditutup.'); st.rerun()
                    except Exception as e: notify_error(e)

elif menu=='Persediaan':
    if require_staff():
        st.title('Persediaan')
        with st.form('item_add'):
            code=st.text_input('Kode Barang'); nm=st.text_input('Nama Barang'); typ=st.selectbox('Jenis Barang',['seed','supply','harvest']); unit=st.text_input('Satuan',value='unit'); save=st.form_submit_button('Tambah Barang')
        if save:
            try: InventoryItem.objects.create(item_code=code,item_name=nm,item_type=typ,unit=unit); st.success('Barang ditambahkan.'); st.rerun()
            except Exception as e: notify_error(e)
        items=list(InventoryItem.objects.filter(is_active=True))
        st.dataframe(df_records(InventoryItem.objects.all(),['id','item_code','item_name','item_type','unit','is_active']),use_container_width=True,hide_index=True)
        with st.form('stock_movement'):
            item=st.selectbox('Barang',items,format_func=str,index=None); mdate=st.date_input('Tanggal Mutasi'); direction=st.selectbox('Jenis Mutasi',['in','out'],format_func=lambda x:'Masuk' if x=='in' else 'Keluar'); qty=st.number_input('Jumlah',min_value=0.001,step=1.0); cost=st.number_input('Biaya per Unit',min_value=0.0,step=100.0); ref=st.text_input('Referensi'); desc=st.text_input('Keterangan'); save=st.form_submit_button('Catat Mutasi')
        if save:
            try: InventoryMovement.objects.create(item=item,movement_date=mdate,movement_type=direction,quantity=Decimal(str(qty)),unit_cost=Decimal(str(cost)),reference=ref,description=desc); st.success('Mutasi dicatat.'); st.rerun()
            except Exception as e: notify_error(e)
        st.subheader('Kartu Stok')
        for it in items:
            res=hitung_stok(it); st.write(f"**{it.item_code} - {it.item_name}: {res['stok_akhir']} {it.unit}**")
        st.dataframe(df_records(InventoryMovement.objects.select_related('item').order_by('-movement_date'),['movement_date','item__item_code','item__item_name','movement_type','quantity','unit_cost','reference','description']),use_container_width=True,hide_index=True)

elif menu=='Produksi & Panen':
    if require_staff():
        st.title('Produksi, Biaya Produksi & Panen')
        with st.form('batch_add'):
            code=st.text_input('Kode Kelompok Tanam'); planting=st.date_input('Tanggal Tanam'); notes=st.text_area('Catatan'); save=st.form_submit_button('Buat Kelompok Tanam')
        if save:
            try: ProductionBatch.objects.create(batch_code=code,planting_date=planting,notes=notes); st.success('Kelompok tanam dibuat.'); st.rerun()
            except Exception as e: notify_error(e)
        batches=list(ProductionBatch.objects.all())
        st.dataframe(df_records(ProductionBatch.objects.all(),['id','batch_code','planting_date','harvest_date','status','notes']),use_container_width=True,hide_index=True)
        growing=[b for b in batches if b.status=='growing']
        if growing:
            with st.form('cost_add'):
                b=st.selectbox('Kelompok Tanam',growing,format_func=str); d=st.date_input('Tanggal Biaya'); desc=st.text_input('Komponen Biaya'); amount=st.number_input('Jumlah Biaya (Rp)',min_value=1.0,step=1000.0); save=st.form_submit_button('Catat Biaya Produksi')
            if save:
                try: ProductionCost.objects.create(batch=b,cost_date=d,description=desc,amount=Decimal(str(amount))); st.success('Biaya dicatat. Hubungkan biaya dengan baris debit WIP (1080) Posted di admin/fitur jurnal sebelum panen.'); st.rerun()
                except Exception as e: notify_error(e)
            st.markdown('**Hubungkan biaya ke jurnal WIP (akun 1080) yang sudah Posted**')
            unlinked=list(ProductionCost.objects.filter(batch__status='growing',journal_line__isnull=True).select_related('batch'))
            wip_lines=list(JournalLine.objects.filter(journal_entry__status='posted',account__account_code='1080').select_related('journal_entry','account'))
            if unlinked and wip_lines:
                with st.form('link_production_cost'):
                    pc=st.selectbox('Biaya Produksi',unlinked,format_func=lambda x:f'{x.batch.batch_code} - {x.description} ({rupiah(x.amount)})')
                    jl=st.selectbox('Baris Debit WIP',wip_lines,format_func=lambda x:f'{x.journal_entry.entry_number} — Debit {rupiah(x.debit)}')
                    link=st.form_submit_button('Hubungkan Biaya ke Jurnal WIP')
                if link:
                    try: pc.journal_line=jl; pc.save(); st.success('Biaya berhasil dihubungkan.'); st.rerun()
                    except Exception as e: notify_error(e)
            elif unlinked: st.info('Belum ada baris jurnal WIP (1080) Posted. Buat dan posting jurnal biaya produksi terlebih dahulu.')
            with st.form('harvest'):
                b=st.selectbox('Batch untuk Dipanen',growing,format_func=str,key='harvest_batch'); harvest_date=st.date_input('Tanggal Panen'); harvest_items=list(InventoryItem.objects.filter(item_type='harvest',is_active=True)); item=st.selectbox('Barang Hasil Panen',harvest_items,format_func=str,index=None); qty=st.number_input('Jumlah Hasil Panen',min_value=0.001,step=1.0); unitcost=st.number_input('Biaya per Unit',min_value=0.01,step=100.0); save=st.form_submit_button('Catat Panen')
            if save:
                try: catat_panen(b.pk,item.pk,harvest_date,qty,unitcost); st.success('Panen dicatat, persediaan masuk dan jurnal transfer biaya dibuat.'); st.rerun()
                except Exception as e: notify_error(e)
        st.subheader('Rincian Biaya per Batch')
        for b in batches:
            result=hitung_biaya_produksi(b.pk); st.write(f"**{b.batch_code}** — Total {rupiah(result['total_biaya'])}; komponen {result['jumlah_komponen']}; belum terhubung jurnal {result['belum_terhubung_jurnal']}")
