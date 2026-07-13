import { getJson } from "./api/client";
import type { AlertRegion, Calendar, RegionScore, Species } from "./api/types";
import { focusRegion } from "./layers";
import { clearMarkers, HEAT_RGB, map, plot } from "./map";
import { dist, errorDetail, escapeHtml, inatUrl, MONTHS, qs, setStatus, state } from "./state";

export function initMonths(): void {
  const box = qs("#months");
  MONTHS.forEach((label, index) => {
    const month = index + 1;
    const button = document.createElement("button");
    button.textContent = label;
    if (state.months.has(month)) button.classList.add("on");
    button.onclick = () => {
      if (state.months.has(month)) {
        state.months.delete(month);
        button.classList.remove("on");
      } else {
        state.months.add(month);
        button.classList.add("on");
      }
    };
    box.appendChild(button);
  });
}

export async function initSpecies(): Promise<void> {
  const species = await getJson<Species[]>("/api/species");
  const select = qs<HTMLSelectElement>("#species");
  species.forEach((entry) => {
    const option = document.createElement("option");
    option.value = String(entry.taxon_id);
    option.textContent = entry.common_name;
    option.title = "View on iNaturalist";
    select.appendChild(option);
  });
}

export function selectedSpecies(): string {
  const chosen = [...qs<HTMLSelectElement>("#species").selectedOptions].map((option) => option.value);
  return chosen.length ? chosen.join(",") : "all";
}

export function monthsParam(): string {
  const ordered = [...state.months].sort((left, right) => left - right);
  return ordered.length ? ordered.join(",") : "1,2,3,4,5,6,7,8,9,10,11,12";
}

interface ChipData {
  taxon_id: number;
  common_name: string;
  label?: string;
}

// common_name/label ultimately come from iNaturalist (user-editable), so escape before
// interpolating into an HTML string template.
const speciesChip = (hit: ChipData, extraClass?: string): string =>
  `<a class="chip${extraClass ? " " + extraClass : ""}" href="${inatUrl(hit.taxon_id)}"
      target="_blank" rel="noopener" onclick="event.stopPropagation()"
   >${escapeHtml(hit.common_name)}${hit.label ? " · " + escapeHtml(hit.label) : ""}</a>`;

export async function runDestinations(): Promise<void> {
  setStatus("Ranking…");
  clearMarkers();
  let regions: RegionScore[];
  try {
    regions = await getJson<RegionScore[]>(
      `/api/destinations?months=${monthsParam()}&species=${selectedSpecies()}`,
    );
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML =
      "<p class='hint'>No regions in range for those months. Try widening months or running Refresh.</p>";
    setStatus("");
    return;
  }
  panel.innerHTML = "";
  regions.forEach((region, rank) => {
    const marker = plot(
      region.center_lat,
      region.center_lng,
      region.score_norm,
      `<b>#${rank + 1}</b> ${dist(region.distance_km)}<br>${region.species.map((hit) => escapeHtml(hit.common_name)).join(", ")}`,
      region.recent_count > 0,
    );
    const card = document.createElement("div");
    card.className = "rank";
    card.innerHTML = `
      <h3><span>#${rank + 1} · ${dist(region.distance_km)}</span><span>${region.n_species} spp</span></h3>
      <div class="bar"><span style="width:${(region.score_norm * 100).toFixed(0)}%"></span></div>
      <div class="meta">score ${region.score_norm.toFixed(2)}${region.recent_count ? ` · ${region.recent_count} seen recently` : ""}</div>
      <div class="chips">${region.species
        .slice(0, 6)
        .map((hit) => speciesChip({ ...hit, label: (hit.w_pheno * 100).toFixed(0) + "%" }))
        .join("")}</div>`;
    card.onclick = () => {
      map.setView([region.center_lat, region.center_lng], 9);
      marker.openPopup();
      focusRegion(region.center_lat, region.center_lng);
      loadCalendar(region.region_id);
    };
    panel.appendChild(card);
  });
  setStatus(`${regions.length} regions`);
}

export async function loadCalendar(regionId: string): Promise<void> {
  let calendar: Calendar;
  try {
    calendar = await getJson<Calendar>(
      `/api/calendar?region_id=${regionId}&species=${selectedSpecies()}`,
    );
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  const peak = Math.max(1, ...Object.values(calendar).map((bucket) => bucket.total));
  let rows = "";
  for (let month = 1; month <= 12; month++) {
    const bucket = calendar[month];
    if (!bucket) continue;
    const fraction = bucket.total / peak;
    const background = `rgba(${HEAT_RGB},${fraction.toFixed(2)})`;
    const speciesText = Object.entries(bucket.species)
      .map(([name, count]) => `${escapeHtml(name)}: ${count}`)
      .join(", ");
    rows += `<tr><td>${MONTHS[month - 1]}</td>
      <td class="heat" style="background:${background}">${bucket.total || ""}</td>
      <td class="meta">${speciesText}</td></tr>`;
  }
  qs("#panel").innerHTML = `<h3 style="margin-top:0">Calendar · region ${regionId}</h3>
    <table class="cal"><tr><th>Month</th><th>Obs</th><th>Species</th></tr>${rows}</table>`;
  setStatus("");
}

export async function runAlerts(): Promise<void> {
  setStatus("Checking recent activity…");
  clearMarkers();
  let regions: AlertRegion[];
  try {
    regions = await getJson<AlertRegion[]>(`/api/alerts?species=${selectedSpecies()}`);
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML = "<p class='hint'>No target species observed in the trailing window yet.</p>";
    setStatus("");
    return;
  }
  panel.innerHTML = "<h3 style='margin-top:0'>Fruiting now / recently</h3>";
  regions.forEach((region) => {
    plot(
      region.center_lat,
      region.center_lng,
      Math.min(1, region.total / 10),
      `${dist(region.distance_km)} · ${region.total} recent`,
      true,
    );
    const card = document.createElement("div");
    card.className = "rank";

    const placeText = region.species[0]?.place_guess
      ? ` · ${escapeHtml(region.species[0].place_guess)}`
      : "";
    card.innerHTML = `<h3><span>${dist(region.distance_km)}${placeText}</span><span>${region.total} recent</span></h3>
      <div class="chips">${region.species
        .map((hit) => {
          const label = hit.count + " · " + hit.last_seen + (hit.obscured ? " ⚠ fuzzy" : "");
          if (hit.uri) {
            return `<a class="chip live" href="${escapeHtml(hit.uri)}"
              target="_blank" rel="noopener" onclick="event.stopPropagation()"
              >${escapeHtml(hit.common_name)} · ${escapeHtml(label)}</a>`;
          }
          return speciesChip({ ...hit, label }, "live");
        })
        .join("")}</div>`;
    card.onclick = () => {
      map.setView([region.center_lat, region.center_lng], 9);
      focusRegion(region.center_lat, region.center_lng);
    };
    panel.appendChild(card);
  });
  setStatus(`${regions.length} active regions`);
}
