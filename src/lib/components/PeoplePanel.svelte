<script lang="ts">
  import { copyText, getJoinInfo, type JoinInfo, type Status } from "$lib/api";
  import { mapError } from "$lib/errors";

  let { st }: { st: Status } = $props();

  let info = $state<JoinInfo | null>(null);
  let busy = $state(false);
  let err = $state("");
  let copied = $state("");

  async function reveal() {
    if (info) {
      info = null;
      return;
    }
    busy = true;
    err = "";
    try {
      info = await getJoinInfo();
    } catch (e) {
      console.error("getJoinInfo failed:", e);
      err = mapError(e);
    } finally {
      busy = false;
    }
  }

  async function copy(what: string, value: string) {
    if (await copyText(value)) {
      copied = what;
      setTimeout(() => (copied = ""), 1600);
    }
  }
</script>

<div class="panel">
  <header>
    <h1>People</h1>
    <p class="dim">
      {st.people_count}
      {st.people_count === 1 ? "person" : "people"} on your box.
    </p>
  </header>

  <section class="card you">
    <span class="dot-ok" aria-hidden="true">&#9679;</span>
    <div>
      <strong>You</strong>
      <span class="dim"> — the owner of this box</span>
    </div>
  </section>

  <section class="card add">
    <div class="add-head">
      <div>
        <h2>Add a person</h2>
        <p class="dim">
          Invite a friend or family member to message you on your box.
        </p>
      </div>
      <button class="btn btn-primary" onclick={reveal} disabled={busy}>
        {busy ? "…" : info ? "Hide" : "Add a person"}
      </button>
    </div>

    {#if err}
      <p class="err">{err}</p>
    {/if}

    {#if info}
      <div class="invite">
        <ol class="steps">
          <li>
            On their phone or computer, install <strong>Element</strong> (or
            Privacy Lodge, once it’s out) — and <strong>Orbot</strong> for Tor.
          </li>
          <li>
            Point it at your box’s address and create an account using the join
            code below.
          </li>
          <li>Say hi — you’ll see them appear here.</li>
        </ol>

        <div class="grid">
          <div class="qr" role="img" aria-label="Join QR code">
            {@html info.svg}
          </div>
          <dl class="creds">
            <dt>Your box address</dt>
            <dd>
              <span class="mono">{info.onion}</span>
              <button class="btn-mini" onclick={() => copy("addr", info!.onion)}>
                {copied === "addr" ? "✓" : "copy"}
              </button>
            </dd>
            <dt>Join code</dt>
            <dd>
              <span class="mono">{info.join_token}</span>
              <button
                class="btn-mini"
                onclick={() => copy("tok", info!.join_token)}
              >
                {copied === "tok" ? "✓" : "copy"}
              </button>
            </dd>
          </dl>
        </div>
        <p class="dim note">
          Anyone with this join code can create an account on your box — share it
          only with people you trust, over a channel they trust.
        </p>
      </div>
    {/if}
  </section>
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--sp-4);
  }
  header h1 {
    font-size: var(--fs-xl);
  }
  header .dim {
    font-size: var(--fs-sm);
    margin-top: var(--sp-1);
  }
  .you {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
  }
  .add-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--sp-4);
  }
  .add-head h2 {
    font-size: var(--fs-md);
  }
  .add-head .dim {
    font-size: var(--fs-sm);
    margin-top: var(--sp-1);
  }
  .invite {
    margin-top: var(--sp-4);
    display: flex;
    flex-direction: column;
    gap: var(--sp-4);
  }
  .steps {
    margin: 0;
    padding-left: 1.2rem;
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
    font-size: var(--fs-sm);
  }
  .grid {
    display: flex;
    gap: var(--sp-5);
    align-items: center;
    flex-wrap: wrap;
  }
  .qr {
    background: #fff;
    border-radius: var(--radius);
    padding: var(--sp-3);
    width: 10rem;
    height: 10rem;
  }
  .qr :global(svg) {
    display: block;
    width: 100%;
    height: 100%;
  }
  .creds {
    margin: 0;
    display: grid;
    gap: var(--sp-1);
  }
  .creds dt {
    font-size: var(--fs-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-top: var(--sp-2);
  }
  .creds dd {
    margin: 0;
    display: flex;
    align-items: center;
    gap: var(--sp-2);
  }
  .creds .mono {
    font-size: var(--fs-sm);
    word-break: break-all;
  }
  .note {
    font-size: var(--fs-xs);
  }
  .err {
    color: var(--err);
    font-size: var(--fs-sm);
  }
</style>
