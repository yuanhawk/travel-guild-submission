# Data Attributions & Licenses — Travel Guild Web

This `web/` directory is the judge-facing frontend for **Travel Guild**. It is
almost entirely original code plus map tiles/data pulled live from OSM at
runtime (no static geodata bundled here — see the root `DATA-ATTRIBUTIONS.md`
for the catalog/geography sources behind the API this app talks to). The one
exception is the **destination hero photography** bundled as static frontend
assets below. Same principle as the engine: honest provenance for everything
we ship, credited here.

---

## 1. Destination hero images (`src/lib/assets/destinations/`)

Landing-page guide-panel destination discovery cards (an internal
image-placement design pass, not shipped in this repo, now covering all
24 destinations across the Asia / Europe / Americas / Africa & Middle East
region tabs — full-bleed background photo + gradient scrim, replacing the
original emoji-icon treatment). One curated photo per demo destination,
sourced exclusively from **Wikimedia Commons** under licenses that explicitly
permit redistribution in a public code repository. Each file below is a
**derivative "1280px" web rendition** of the Commons original (generated via
Commons' own thumbnail service — the same photographic content, resized, not
re-licensed) to keep the bundle a reasonable size; the linked Commons file
page has the original full-resolution file and its full license/history
record.

The small **112px-square thumbnails** actually rendered in the guide cards
(`src/lib/assets/destinations/thumbs/*.jpg`, ~2–15KB each, generated locally
with `sharp` from the 1280px files below) are the same photographic content
again resized, not separately sourced or re-licensed — the attributions below
cover both.

| # | File | Destination | Source (Commons file) | License | Attribution |
|---|------|-------------|------------------------|---------|--------------|
| 1 | `japan.jpg` | Japan (Fushimi Inari torii path, Kyoto) | [File:Torii path with lantern at Fushimi Inari Taisha Shrine, Kyoto, Japan.jpg](https://commons.wikimedia.org/wiki/File:Torii_path_with_lantern_at_Fushimi_Inari_Taisha_Shrine,_Kyoto,_Japan.jpg) | **CC BY-SA 4.0** | © Basile Morin, via Wikimedia Commons, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) |
| 2 | `bali.jpg` | Bali, Indonesia (Tegalalang / Subak Ceking rice terraces) | [File:Tegalalang Rice Terrace - Subak Ceking on Bali 11.jpg](https://commons.wikimedia.org/wiki/File:Tegalalang_Rice_Terrace_-_Subak_Ceking_on_Bali_11.jpg) | **CC BY 4.0** | © Anggabuana, via Wikimedia Commons, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0) |
| 3 | `thailand.jpg` | Thailand (Wat Arun, Bangkok, at sunset from the Chao Phraya) | [File:Wat Arun on the sunset.jpg](https://commons.wikimedia.org/wiki/File:Wat_Arun_on_the_sunset.jpg) | **CC BY-SA 4.0** | © Trip.with.taste, via Wikimedia Commons, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) |
| 4 | `vietnam.jpg` | Vietnam (Ha Long Bay karst islands) | [File:The karsts stretch as far as the eye can see (31263636870).jpg](https://commons.wikimedia.org/wiki/File:The_karsts_stretch_as_far_as_the_eye_can_see_(31263636870).jpg) | **CC BY 2.0** | © shankar s., via Wikimedia Commons (originally Flickr), licensed under [CC BY 2.0](https://creativecommons.org/licenses/by/2.0) |
| 5 | `singapore.jpg` | Singapore (Supertree Grove, Gardens by the Bay, aerial) | [File:Supertree Grove, Gardens by the Bay, Singapore1.jpg](https://commons.wikimedia.org/wiki/File:Supertree_Grove,_Gardens_by_the_Bay,_Singapore1.jpg) | **CC0 1.0** (public domain) | Mustang Joe, via Wikimedia Commons — [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/deed.en) (no attribution legally required; credited anyway per project convention) |
| 6 | `south-korea.jpg` | South Korea (Gyeonghoeru Pavilion, Gyeongbokgung Palace, Seoul) | [File:Gyeonghoeru (Royal Banquet Hall) at Gyeongbokgung Palace, Seoul.jpg](https://commons.wikimedia.org/wiki/File:Gyeonghoeru_(Royal_Banquet_Hall)_at_Gyeongbokgung_Palace,_Seoul.jpg) | **CC BY-SA 4.0** | © Frank Schulenburg, via Wikimedia Commons, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) |
| 7 | `france.jpg` | France (Eiffel Tower, Paris) | [File:Eiffel Tower in 2022 02.jpg](https://commons.wikimedia.org/wiki/File:Eiffel_Tower_in_2022_02.jpg) | **CC BY-SA 4.0** | © Maksim Sokolov (maxergon.com), via Wikimedia Commons, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) |
| 8 | `spain.jpg` | Spain (Sagrada Família, Barcelona) | [File:Sagrada Familia in Barcelona 10.jpg](https://commons.wikimedia.org/wiki/File:Sagrada_Familia_in_Barcelona_10.jpg) | **CC BY 4.0** | © Ronny Siegel, via Wikimedia Commons, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0) |
| 9 | `italy.jpg` | Italy (Colosseum, Rome) | [File:Colosseum of Rome, Italy.jpg](https://commons.wikimedia.org/wiki/File:Colosseum_of_Rome,_Italy.jpg) | **CC0 1.0** (public domain) | Wilfredor, via Wikimedia Commons — [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/deed.en) (no attribution legally required; credited anyway per project convention) |
| 10 | `greece.jpg` | Greece (Oia sunset, Santorini) | [File:Oia Sunset - Santorini, Greece - August 2008.jpg](https://commons.wikimedia.org/wiki/File:Oia_Sunset_-_Santorini,_Greece_-_August_2008.jpg) | **Public domain** | Saolo996, via Wikimedia Commons — public domain (no attribution legally required; credited anyway per project convention) |
| 11 | `portugal.jpg` | Portugal (Dom Luís I Bridge, Porto) | [File:Dom Luis I bridge from Jardim do Morro (1).jpg](https://commons.wikimedia.org/wiki/File:Dom_Luis_I_bridge_from_Jardim_do_Morro_(1).jpg) | **CC BY-SA 4.0** | © Krzysztof Golik, via Wikimedia Commons, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) |
| 12 | `netherlands.jpg` | Netherlands (canal houses, Amsterdam) | [File:Colorful Amsterdam - Flickr - radkuch.13.jpg](https://commons.wikimedia.org/wiki/File:Colorful_Amsterdam_-_Flickr_-_radkuch.13.jpg) | **CC BY 2.0** | © Radek Kucharski, via Wikimedia Commons (originally Flickr), licensed under [CC BY 2.0](https://creativecommons.org/licenses/by/2.0) |
| 13 | `usa.jpg` | USA (Golden Gate Bridge, San Francisco, at sunset) | [File:Golden Gate Bridge at sunset 1.jpg](https://commons.wikimedia.org/wiki/File:Golden_Gate_Bridge_at_sunset_1.jpg) | **CC BY-SA 3.0** | © Brocken Inaglory, via Wikimedia Commons, licensed under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0) |
| 14 | `mexico.jpg` | Mexico (El Castillo / Pyramid of Kukulcán, Chichén Itzá) | [File:El Castillo Stitch 2008 Edit 2.jpg](https://commons.wikimedia.org/wiki/File:El_Castillo_Stitch_2008_Edit_2.jpg) | **CC BY-SA 3.0** | © Fcb981, via Wikimedia Commons, licensed under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0) |
| 15 | `colombia.jpg` | Colombia (clock tower gate at sunset, Cartagena) | [File:Sunset-cartagena-tower-dewired.jpg](https://commons.wikimedia.org/wiki/File:Sunset-cartagena-tower-dewired.jpg) | **CC BY-SA 2.0** | © Igvir Ramirez, via Wikimedia Commons, licensed under [CC BY-SA 2.0](https://creativecommons.org/licenses/by-sa/2.0) |
| 16 | `peru.jpg` | Peru (Machu Picchu) | [File:80 - Machu Picchu - Juin 2009 - edit.jpg](https://commons.wikimedia.org/wiki/File:80_-_Machu_Picchu_-_Juin_2009_-_edit.jpg) | **CC BY-SA 3.0** | © Martin St-Amant (S23678), via Wikimedia Commons, licensed under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0) |
| 17 | `canada.jpg` | Canada (Lake Louise, Banff National Park) | [File:1 lake louise pano 2019.jpg](https://commons.wikimedia.org/wiki/File:1_lake_louise_pano_2019.jpg) | **CC BY-SA 4.0** | © Chensiyuan, via Wikimedia Commons, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) |
| 18 | `costa-rica.jpg` | Costa Rica (Arenal Volcano) | [File:Arenal Volcano 01.jpg](https://commons.wikimedia.org/wiki/File:Arenal_Volcano_01.jpg) | **CC0 1.0** (public domain) | Bernard Gagnon, via Wikimedia Commons — [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/deed.en) (no attribution legally required; credited anyway per project convention) |
| 19 | `dubai.jpg` | Dubai, UAE (skyline with Burj Khalifa) | [File:Dubai Skyline mit Burj Khalifa (18241030269).jpg](https://commons.wikimedia.org/wiki/File:Dubai_Skyline_mit_Burj_Khalifa_(18241030269).jpg) | **CC BY 2.0** | © Tim Reckmann, via Wikimedia Commons, licensed under [CC BY 2.0](https://creativecommons.org/licenses/by/2.0) |
| 20 | `morocco.jpg` | Morocco (Chefchaouen, the blue city) | [File:Chefchaouen - blue city in Morocco.jpg](https://commons.wikimedia.org/wiki/File:Chefchaouen_-_blue_city_in_Morocco.jpg) | **CC BY-SA 4.0** | © Ekaterina Kvelidze, via Wikimedia Commons, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) |
| 21 | `egypt.jpg` | Egypt (Pyramids of Giza) | [File:All Gizah Pyramids.jpg](https://commons.wikimedia.org/wiki/File:All_Gizah_Pyramids.jpg) | **CC BY-SA 2.0** | © Ricardo Liberato, via Wikimedia Commons, licensed under [CC BY-SA 2.0](https://creativecommons.org/licenses/by-sa/2.0) |
| 22 | `kenya.jpg` | Kenya (elephant family, Maasai Mara) | [File:Gentle Giants, A Family of Five Elephants in Maasai Mara - Flickr - . Ray in Manila.jpg](<https://commons.wikimedia.org/wiki/File:Gentle_Giants,_A_Family_of_Five_Elephants_in_Maasai_Mara_-_Flickr_-_._Ray_in_Manila.jpg>) | **CC BY 2.0** | © . Ray in Manila, via Wikimedia Commons (originally Flickr), licensed under [CC BY 2.0](https://creativecommons.org/licenses/by/2.0) |
| 23 | `jordan.jpg` | Jordan (Al-Khazneh / The Treasury, Petra) | [File:Al-Khazneh (The Treasury), Petra, Jordan.jpg](https://commons.wikimedia.org/wiki/File:Al-Khazneh_(The_Treasury),_Petra,_Jordan.jpg) | **CC BY 4.0** | © Vyacheslav Argenberg, via Wikimedia Commons, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0) |
| 24 | `south-africa.jpg` | South Africa (Table Mountain, Cape Town) | [File:Cape Town (ZA), Table Mountain -- 2024 -- 2825.jpg](https://commons.wikimedia.org/wiki/File:Cape_Town_(ZA),_Table_Mountain_--_2024_--_2825.jpg) | **CC BY-SA 4.0** | © Dietmar Rabich, via Wikimedia Commons, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) |

**Verification note.** Every license above was confirmed by reading the file
page's own license/`extmetadata` (via the Commons API's
`prop=imageinfo&iiprop=extmetadata`, not assumed from search-result
snippets). None are non-commercial-only or no-derivatives-restricted — all 24
permit commercial use and redistribution, which is what a public hackathon
submission repo requires. CC BY / CC BY-SA require attribution (given in the
table above); CC0/public-domain requires none but is credited anyway for
consistency.

**Scope note.** This table covers all 24 destination hero images that back
the region-tab browse cards (Asia ×6, Europe ×6, Americas ×6, Africa & Middle
East ×6) — the full set REGION_TABS in `src/App.svelte` needs. A further ~9
destinations appear only in the persona-recommendation lists (Nepal, New
Zealand, Iceland, Australia, Maldives, Seychelles, French Riviera, Chiang
Mai, Medellín) and were treated as optional/time-permitting per the sourcing
plan; they currently fall back to the pre-existing emoji-icon treatment
rather than a sourced photo. It does not (yet) cover the per-item/per-day-tab
thumbnails from that same internal design pass — those are speculative,
unbuilt, and would route through the live `/place_card` photo backend rather
than being bundled static assets, so they don't need a static attribution
entry here.

---

## 2. Everything else

Map tiles and vector data rendered by MapLibre GL are fetched live from
free OpenStreetMap-based tile sources at runtime — not bundled in this repo —
and are © OpenStreetMap contributors, ODbL 1.0
(https://www.openstreetmap.org/copyright). All catalog data (cities, POIs,
pricing, risk/advisory, etc.) is served by the engine at this repo's root
over the API contract; see the root `DATA-ATTRIBUTIONS.md` for the full
source list and honesty disclosures (seeded-vs-live status, simulated
settlement, etc.) — this frontend has no independent data sources of its own
to disclose beyond the hero images above.
