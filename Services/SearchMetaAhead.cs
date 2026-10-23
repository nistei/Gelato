using Microsoft.Extensions.Logging;

namespace Gelato.Services;

/// <summary>
/// Asks for the full metas of a search's first movies and series, and the movies' streams, while
/// the user picks one (<see cref="Config.PluginConfiguration.SearchMetaAhead"/>). Opening a search
/// result waits for both, the streams 0.5 to 1.5 s and the meta up to 3 s, and users take 6 to
/// 9 s from the search to the click. A series' streams are an episode's, asked for when one
/// plays.
/// </summary>
/// <remarks>
/// Jellyfin Web searches at every pause in the typing ("Bauer", "Bauer such", "Bauer sucht
/// frau"), so a search counts once it has settled: nothing newer from the same user for
/// <see cref="SettleDelay"/>. A newer search with another term drops the waiting one; one with
/// the same term (the client's other requests for it, a second page) leaves it as it is.
/// </remarks>
public sealed class SearchMetaAhead(Lazy<GelatoManager> manager, ILogger<SearchMetaAhead> log)
{
    /// <summary>How many movies and how many series of a search have their meta asked for.</summary>
    public const int Titles = 3;

    internal static TimeSpan SettleDelay { get; set; } = TimeSpan.FromSeconds(1);

    private readonly Lock _gate = new();
    private readonly Dictionary<Guid, (string Term, CancellationTokenSource Cts)> _waiting = [];

    /// <summary>
    /// Takes a search's answer: <paramref name="results"/> are its results the library does not
    /// hold, by their search ids, best first. Never waits.
    /// </summary>
    public void Searched(
        Guid userId,
        string term,
        IReadOnlyList<(Guid SearchId, StremioMeta Meta)> results,
        GelatoStremioProvider stremio
    )
    {
        CancellationTokenSource cts;
        lock (_gate)
        {
            if (_waiting.TryGetValue(userId, out var waiting))
            {
                if (string.Equals(waiting.Term, term, StringComparison.OrdinalIgnoreCase))
                    return;

                log.LogDebug(
                    "SearchMetaAhead: \"{Term}\" was followed by \"{Next}\", nothing asked for it",
                    waiting.Term,
                    term
                );
                waiting.Cts.Cancel();
                _waiting.Remove(userId);
            }

            if (results.Count == 0)
                return;

            cts = new CancellationTokenSource();
            _waiting[userId] = (term, cts);
        }

        var titles = results.ToList();
        _ = Task.Run(async () =>
        {
            try
            {
                await Task.Delay(SettleDelay, cts.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                cts.Dispose();
                return;
            }

            lock (_gate)
            {
                if (_waiting.TryGetValue(userId, out var current) && current.Cts == cts)
                    _waiting.Remove(userId);
            }
            cts.Dispose();

            log.LogDebug(
                "SearchMetaAhead: \"{Term}\" settled, asking for the meta and streams of {Count} result(s)",
                term,
                titles.Count
            );
            // A result opened while the search settled has what it needed: its open asked.
            foreach (var (searchId, meta) in titles)
            {
                if (manager.Value.GetInsertedId(searchId) is not null)
                    continue;

                stremio.StartMetaAhead(meta);
                // Taken by the stream sync of the open within 30 seconds, dropped after that.
                manager.Value.StartStreamSyncAhead(meta, userId);
            }
        });
    }
}
