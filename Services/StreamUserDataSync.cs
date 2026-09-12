using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Entities;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Gelato.Services;

/// <summary>
/// Keeps the watch state of a movie/episode in step with its stream rows.
/// </summary>
/// <remarks>
/// Jellyfin 12 keeps a resume point on the version that was played, because local versions can be
/// different cuts. A movie's streams are the same film from different sources, and the list
/// changes between syncs, so the resume point, favourite and rating set on a stream are copied to
/// the movie. The movie holds the shared state, and DtoServiceDecorator shows it on every version.
/// Played state needs no copy: Jellyfin marks every version itself.
/// </remarks>
public sealed class StreamUserDataSync(
    IUserDataManager userDataManager,
    IUserManager userManager,
    ILibraryManager libraryManager,
    ILogger<StreamUserDataSync> log
) : IHostedService
{
    public Task StartAsync(CancellationToken cancellationToken)
    {
        userDataManager.UserDataSaved += OnUserDataSaved;
        return Task.CompletedTask;
    }

    public Task StopAsync(CancellationToken cancellationToken)
    {
        userDataManager.UserDataSaved -= OnUserDataSaved;
        return Task.CompletedTask;
    }

    private void OnUserDataSaved(object? sender, UserDataSaveEventArgs e)
    {
        // Rows being deleted are unlinked first, so clearing their watch state is not copied.
        if (
            e.Item is not Video { PrimaryVersionId: { } primaryId } row
            || !row.HasStreamTag()
            || e.SaveReason is UserDataSaveReason.TogglePlayed or UserDataSaveReason.Import
        )
        {
            return;
        }

        try
        {
            if (
                userManager.GetUserById(e.UserId) is not { } user
                || libraryManager.GetItemById(primaryId) is not Video primary
                || userDataManager.GetUserData(user, primary) is not { } data
            )
            {
                return;
            }

            var source = e.UserData;
            if (e.SaveReason is UserDataSaveReason.UpdateUserRating or UserDataSaveReason.UpdateUserData)
            {
                data.IsFavorite = source.IsFavorite;
                data.Likes = source.Likes;
                data.Rating = source.Rating;
            }

            if (e.SaveReason is not UserDataSaveReason.UpdateUserRating)
            {
                data.PlaybackPositionTicks = source.PlaybackPositionTicks;
                data.LastPlayedDate = source.LastPlayedDate ?? data.LastPlayedDate;
                // A finished playback marks every version played through Jellyfin.
                if (e.SaveReason is UserDataSaveReason.UpdateUserData)
                {
                    data.Played = source.Played;
                }
            }

            // Not a playback reason: the movie was not played, and listeners on playback reasons
            // would count the stream's playback twice.
            userDataManager.SaveUserData(
                user,
                primary,
                data,
                e.SaveReason is UserDataSaveReason.UpdateUserRating
                    ? UserDataSaveReason.UpdateUserRating
                    : UserDataSaveReason.UpdateUserData,
                CancellationToken.None
            );
        }
        catch (Exception ex)
        {
            log.LogWarning(
                ex,
                "Could not copy watch state of stream {Id} to {PrimaryId}",
                row.Id,
                primaryId
            );
        }
    }
}
