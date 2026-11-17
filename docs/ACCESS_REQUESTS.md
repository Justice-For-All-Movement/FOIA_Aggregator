# Getting compliant access to sites that refuse the crawler

Updated 2026-11-17. The crawler identifies itself honestly, honours robots.txt and keeps polite
per-host delays. It does not disguise itself as a browser, solve CAPTCHAs, rotate IPs or proxies,
or otherwise get around access controls. Sites that refuse it are recorded as `BLOCKED`. The
routes below get those sites covered without breaking their rules.

## 1. Identify an accountable contact (owner: set one repository variable)
Set the repository variable `FOIA_CRAWLER_CONTACT` to an e-mail address that someone reads.
The crawler then sends it in its User-Agent and in the standard `From` header (RFC 9110):

    FOIAArchiveBot/0.1 (+https://github.com/Justice-For-All-Movement/FOIA_Aggregator; foia@your-domain)

SEC's EDGAR fair-access policy requires this (a declared user agent with a contact e-mail), and
other bot filters often treat an identified crawler better.

## 2. Ask each blocked site to allowlist the crawler (owner sends; draft below)
Most 403s come from web application firewalls that block every request from cloud data
centres, not from a decision to keep FOIA material from the public. The site owner can
allowlist the crawler by User-Agent, or point us to a feed or API.

Template (one per agency web team or FOIA public liaison):

> Subject: Request to allow a public-interest crawler to access your FOIA reading room / FOIA logs
>
> I run the FOIA Aggregator (https://github.com/Justice-For-All-Movement/FOIA_Aggregator), a
> free public index of records federal agencies have released under FOIA. Our crawler reads
> your public FOIA pages at [URL] but receives HTTP 403 from your firewall.
>
> The crawler identifies itself as `FOIAArchiveBot` with this contact address, follows
> robots.txt, makes at most one request every two seconds to your site, and only reads public
> pages and released records. Could you allow this user agent, or tell us about a feed,
> API or bulk download for your FOIA library and FOIA logs?
>
> 5 U.S.C. 552(a)(2) requires agencies to make these records available for public inspection
> in electronic format. Thank you.
>
> [name, outlet, contact]

## 3. Blocked in the 2026-11-17 runs

**HTTP 403 (reading rooms, Catalog crawl run 43983891639):**
www.usda.gov, www.rd.usda.gov, www.fsis.usda.gov, www.dol.gov, www.osha.gov, www.bls.gov,
www.transportation.gov, highways.dot.gov, railroads.dot.gov, www.transit.dot.gov,
www.fmcsa.dot.gov, www.phmsa.dot.gov, www.marad.dot.gov, www.seaway.dot.gov, www.nhtsa.gov,
www.hhs.gov, www.hrsa.gov, www.ftc.gov, www.flra.gov, www.usmint.gov, www.usmarshals.gov,
www.dea.gov, www.atf.gov, www.tva.com, www.ssa.gov, www.sec.gov, www.peacecorps.gov,
www.noaa.gov, www.commerce.gov, www.hq.nasa.gov, www.hudoig.gov, www.ffiec.gov, www.fcc.gov,
www.ed.gov, www.doi.gov, www.dnfsb.gov, www.acus.gov, www.amtrak.com, iaf.gov, americorps.gov.

The DoD/IC hosts below reject all cloud traffic: www.nsa.gov, www.nro.gov, www.nga.mil,
www.dia.mil, www.dodig.mil, www.esd.whs.mil, www.dcsa.mil, www.dla.mil, www.dfas.mil,
www.dtra.mil, www.dcma.mil, www.dcaa.mil, www.dodea.edu, www.jpra.mil, www.asbca.mil,
foia.army.mil, www.usainscom.army.mil, www.compliance.af.mil, www.oni.navy.mil,
www.hqmc.marines.mil, www.nationalguard.mil, www.dcms.uscg.mil, www.cybercom.mil,
www.pacom.mil, www.stratcom.mil, www.spacecom.mil, www.southcom.mil, www.northcom.mil,
www.eucom.mil, www.africom.mil.

**Published FOIA log pages refused (PR #91 live dry run):** SEC, ICE, Coast Guard, DNFSB, DCSA,
EUCOM, OSD/Joint Staff, CYBERCOM, FMCSA, FRA, MARAD, DOT OST, PHMSA, Education, FTC, FSIS,
IHS (CAPTCHA), State (robots.txt disallows the log page).

**robots.txt unreachable (TLS/DNS errors, so the crawler treats the site as disallowed; retried each run):**
www.cia.gov, energy.gov, www.health.mil, www.socom.mil, www.ustranscom.mil, www3.centcom.mil,
www.fsa.usda.gov, www.fpacbc.usda.gov, www.swpa.gov, www.wipp.energy.gov.

## 4. Every agency whose log stays unreachable gets a log request
Send_FOIA_Log_Requests `plan-logs --published-logs <file>` skips only the agencies listed in that
file. List only sources whose state is `OK` in `published_log_sources`, so every blocked agency
gets a periodic FOIA request for its closed-request log, the same as CIA. To do: an export that
maps aggregator agency slugs to FOIA.gov abbreviations, so this file is generated automatically.

## Owner decisions still open
* **Internet Archive copies:** fetch the Wayback Machine's public copies of blocked log
  pages and files, labelled with the snapshot date and URL. The code change was stopped
  pending your decision, because it reaches content the agency's own site refuses to serve
  to our crawler.
* **Self-hosted runner:** run the crawl from a normal (non-data-centre) internet connection,
  with the same honest identification and rate limits.
