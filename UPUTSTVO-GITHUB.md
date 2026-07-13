# Miss Sync na svaka 4 sata

## 1. Napravi javni GitHub repozitorijum

Na GitHubu klikni **New repository**, upiši naziv, na primer `miss-sync`, izaberi **Public** i napravi repozitorijum.

Otpakuj ZIP paket i postavi njegov kompletan sadržaj u koren repozitorijuma. Obavezno postavi i skriveni folder `.github`.

## 2. Dodaj Shopify token kao Secret

U repozitorijumu otvori:

**Settings → Secrets and variables → Actions → New repository secret**

Unesi:

- Name: `SHOPIFY_TOKEN`
- Secret: Shopify Admin API token

Token nemoj unositi u `miss_sync.py`, jer je sadržaj javnog repozitorijuma svima vidljiv.

## 3. Uključi Actions

Otvori karticu **Actions**. Ako GitHub traži potvrdu, klikni da omogućiš workflow.

Workflow se pokreće automatski na svaka 4 sata. Vremena su 00:17, 04:17, 08:17, 12:17, 16:17 i 20:17 po UTC vremenu. GitHub može ponekad pokrenuti zakazani posao nekoliko minuta kasnije.

## 4. Ručno pokretanje

Otvori:

**Actions → Miss Sync → Run workflow → Run workflow**

Tu možeš ručno pokrenuti sinhronizaciju u bilo kom trenutku.

## 5. Provera rezultata

U kartici **Actions** otvori poslednje pokretanje i zatim posao **sync**. Tu se vidi kompletan log skripte.

Jedno pokretanje ima ograničenje od četiri sata u workflow fajlu. Tvoj trenutni rad od oko sat i po staje unutar tog ograničenja.
