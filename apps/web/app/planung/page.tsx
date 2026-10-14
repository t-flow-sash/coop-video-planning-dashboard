import Link from "next/link";
export default function PlanungPage() {
  return (
    <div className="simple-page">
      <h1>Planung</h1>
      <p>
        Terminierte Veröffentlichungen werden aus den vorhandenen Kooperationen
        abgeleitet. In M1 erfolgt die operative Arbeit in der Kooperationsliste.
      </p>
      <Link className="primary-link" href="/coops?sort=release">
        Nach Release-Datum sortierte Kooperationen öffnen
      </Link>
    </div>
  );
}
