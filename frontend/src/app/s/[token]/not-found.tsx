/** Unknown, malformed or revoked share links. */
export default function ShareNotFound() {
  return (
    <div className="mx-auto flex w-full max-w-xl flex-col gap-3 px-6 py-24">
      <h1 className="text-2xl font-semibold text-foreground">This link is no longer available</h1>
      <p className="text-base text-muted-foreground">
        The person who shared it may have removed it. Ask them for a new link.
      </p>
    </div>
  );
}
