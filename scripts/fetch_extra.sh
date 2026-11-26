#!/usr/bin/env bash
# Enrichment datasets (all keyless, checked live 2026-11-23) → data/extra/. ~15 MB total.
set -uo pipefail
mkdir -p data/extra && cd data/extra
NYC=https://data.cityofnewyork.us/resource
# Open-data portals answer transient 503s: retry those (curl retries 408/429/5xx and timeouts) with a pause.
RETRY=(--retry 4 --retry-delay 15 --retry-connrefused)
get() { # get <outfile> <base-url> [--data-urlencode args...]
  local out=$1 url=$2; shift 2
  curl -fsS -m 240 "${RETRY[@]}" -G "$url" "$@" -o "$out.part" && mv "$out.part" "$out" && echo "ok   $out $(wc -l < "$out") lines" || echo "FAIL $out"
}
get hpd_elevator.csv   $NYC/ygpa-z7cr.csv --data-urlencode '$select=bbl,latitude,longitude,count(distinct complaint_id) as n' --data-urlencode "\$where=major_category='ELEVATOR' AND received_date>='2025-11-01' AND latitude IS NOT NULL" --data-urlencode '$group=bbl,latitude,longitude' --data-urlencode '$limit=50000' &
get lotteries.csv      $NYC/vy5i-a666.csv --data-urlencode '$select=lottery_id,lottery_name,lottery_status,lottery_start_date,lottery_end_date,unit_count,lottery_mobility_percent,postcode,latitude,longitude' --data-urlencode '$limit=5000' &
get bus_stops.csv      https://data.ny.gov/resource/2ucp-7wg5.csv --data-urlencode '$select=stop_id,route_id,min(latitude) as lat,min(longitude) as lon' --data-urlencode "\$where=in_effect='true'" --data-urlencode '$group=stop_id,route_id' --data-urlencode '$limit=60000' &
get places_zcta.csv    https://data.cdc.gov/resource/kee5-23sr.csv --data-urlencode '$select=zcta5,totalpop18plus,mobility_crudeprev,disability_crudeprev,loneliness_crudeprev,lacktrpt_crudeprev,housinsecu_crudeprev' --data-urlencode "\$where=zcta5 between '10001' and '11697'" --data-urlencode '$limit=600' &
wait
get ped_crashes.csv    $NYC/h9gi-nx95.csv --data-urlencode '$select=latitude,longitude,number_of_pedestrians_injured,number_of_pedestrians_killed' --data-urlencode "\$where=crash_date>='2025-11-01' AND (number_of_pedestrians_injured>0 OR number_of_pedestrians_killed>0) AND latitude IS NOT NULL" --data-urlencode '$limit=50000' &
get restrooms.csv      $NYC/i7jb-7jku.csv --data-urlencode '$select=facility_name,location_type,status,accessibility,latitude,longitude' --data-urlencode "\$where=status='Operational'" --data-urlencode '$limit=5000' &
get aps.csv            $NYC/de3m-c5p4.csv --data-urlencode '$select=point_x,point_y,date_insta' --data-urlencode '$limit=10000' &
get health.csv         https://health.data.ny.gov/resource/vn5v-hh5r.csv --data-urlencode '$select=facility_name,description,fac_zip,latitude,longitude' --data-urlencode "\$where=county in('Kings','Queens','Bronx','New York','Richmond') AND description in('Hospital','Diagnostic and Treatment Center','Hospital Extension Clinic','Diagnostic and Treatment Center Extension Clinic')" --data-urlencode '$limit=5000' &
wait
# reported felonies, year to date (opt-in display layer only — never scored)
get felonies.csv       $NYC/5uac-w243.csv --data-urlencode '$select=latitude,longitude' --data-urlencode "\$where=law_cat_cd='FELONY' AND latitude>40" --data-urlencode '$limit=400000' &
curl -fsS -m 240 "${RETRY[@]}" -o gtfs_subway.zip https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip && echo "ok   gtfs_subway.zip $(wc -c < gtfs_subway.zip) bytes" || echo "FAIL gtfs_subway.zip"
wait
ls -la
# live elevator status + equipment list (keyless) and MTA capital projects that add accessibility
MTA="https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2F"
for f in nyct_ene nyct_ene_equipments; do curl -fsS -m 60 "${RETRY[@]}" -o $f.json "$MTA$f.json" && echo "ok   $f.json" || echo "FAIL $f.json"; done
curl -fsS -m 60 "${RETRY[@]}" -G https://data.ny.gov/resource/9hy6-8j6t.json --data-urlencode "\$where=upper(title) like '%ADA%' OR upper(description) like '%ACCESSIB%'" --data-urlencode '$select=project_id,title,stage,description,estimated_actual_completion_date' -o capital_ada.json && echo "ok   capital_ada.json" || echo "FAIL capital_ada.json"
[ -d gtfs ] || (mkdir -p gtfs && unzip -o -q gtfs_subway.zip -d gtfs)
