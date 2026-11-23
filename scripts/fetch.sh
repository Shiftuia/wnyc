#!/usr/bin/env bash
# Downloads everything the app needs into ./data. ~0.55 GB, mostly the HVFHV month.
set -euo pipefail
mkdir -p data && cd data
T=https://d37ci6vzurychx.cloudfront.net
curl -fL -o fhvhv_2026-09.parquet  $T/trip-data/fhvhv_tripdata_2026-09.parquet &   # 510 MB, 21.0M trips
curl -fL -o taxi_zones.zip         $T/misc/taxi_zones.zip
curl -fL -o taxi_zone_lookup.csv   $T/misc/taxi_zone_lookup.csv
curl -fL -o zori_zip.csv https://files.zillowstatic.com/research/public_csvs/zori/Zip_zori_uc_sfrcondomfr_sm_month.csv
curl -fL -o subway_stations.csv 'https://data.ny.gov/resource/39hk-dx4f.csv?$limit=5000'
curl -fL -o elevator_availability.csv "https://data.ny.gov/resource/rc78-7x78.csv?\$where=equipment_type='Elevator'%20and%20month>='2026-04-01'&\$limit=50000"
# curb ramps: 217,679 rows. One big request gets cut off mid-file, so page it and keep only the columns we use.
for off in 0 110000; do
  curl -fL -o ramps_$off.csv "https://data.cityofnewyork.us/resource/ufzp-rrqu.csv?\$select=the_geom,rampid,borough,dws_conditions,ponding,obstacles_ramp&\$order=rampid&\$limit=110000&\$offset=$off" &
done
curl -fL -o ramp_complaints.csv "https://data.cityofnewyork.us/resource/jagj-gttd.csv?\$where=complaint_date>='2025-01-01'&\$limit=200000" &
# PLUTO: only multifamily residential lots (>=4 units), the columns we need
curl -fL -o pluto_multifamily.csv "https://data.cityofnewyork.us/resource/64uk-42ks.csv?\$select=bbl,zipcode,yearbuilt,numfloors,unitsres,latitude,longitude,bldgclass&\$where=unitsres>=4&\$limit=300000" &
wait
ls -la
