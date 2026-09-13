# Tanulsagok a comma 4 eSIM kiserletbol

- A helyettesito profilt elobb telepiteni es aktivva tenni kell; aktiv factory profil torlese utan nem szabad masik profilt keresni.
- A profil engedelyezett allapota, a modem ICCID-je, az LTE-regisztracio, a packet attach, a PDP, a `ppp0`, az utvonal, a DNS es a cellularis HTTPS kulon bizonyiteki retegek.
- A `GsmApn` Params erteke nem ugyanaz a bizonyitek, mint a modem `CGDCONT` allapota.
- A Telekom kontrollalt mintaban LTE-regisztraciot, attach-ot, IPv4-cimet es DNS-t kapott, majd a kartya visszavaltott Webbingre. Ez a szolgaltatoi profil es APN mukodokepesseget bizonyitotta, nem a tartossagot.
- A Webbing torlese a visszavaltasi celpontot szuntette meg. Nem bizonyitotta a WebbingCTRL Manual modot vagy barmilyen applet-policy valtozast.
- Mutacios szandekot nem szabad "elkuldve" allapotkent naplozni a logikai csatorna megnyitasa elott.
- Bizonytalan valasz utan nincs automatikus ujrakuldes. Elobb friss profil-listaval kell egyeztetni; ha a cel meg jelen van, operatori dontes szukseges.
- A normal factory-delete vedelmet meg kell tartani. Az elteres csak kulon, explicit, destruktiv labor-eszkozben elfogadhato.
- A sikeres torles teljes torteneti parancssora es a radio ujraengedelyezesenek pontos parancsa nem maradt fenn. Ezeket nem helyettesitjuk kitalalt `CFUN` paranccsal.
- Az uj helper csak offline fixture-okon validalt. A torteneti hardversiker nem azonositja byte-pontosan az uj kodot.
