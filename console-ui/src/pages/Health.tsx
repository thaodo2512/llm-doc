import { api } from "../api";
import { FullSpinner, PageTitle, Pre, useAsync } from "../components/ui";

export function Health() {
  const doctor = useAsync(() => api.doctor(), []);
  const inventory = useAsync(() => api.inventory(), []);

  return (
    <div>
      <PageTitle
        title="Health"
        subtitle="docmcp.sh doctor + corpus inventory"
        actions={
          <button className="btn btn-ghost" onClick={() => { doctor.reload(); inventory.reload(); }}>
            Re-check
          </button>
        }
      />
      <div className="card">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="font-semibold">doctor</h2>
          {doctor.data && (doctor.data.ok ? <span className="badge-ok">healthy</span> : <span className="badge-bad">unhealthy</span>)}
        </div>
        {doctor.loading ? <FullSpinner /> : <Pre text={doctor.data?.output || doctor.error} />}
      </div>
      <div className="card mt-4">
        <h2 className="mb-2 font-semibold">Inventory</h2>
        {inventory.loading ? <FullSpinner /> : <Pre text={inventory.data?.output || inventory.error} />}
      </div>
    </div>
  );
}
