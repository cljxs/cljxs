# Draft answers: YouTube API Services audit form

The form is at https://support.google.com/youtube/contact/yt_api_form.
Until the audit passes, YouTube keeps every upload from this project
private. Replace anything in <angle brackets> with your own details.

**API client / project:** your Google Cloud project ID, shown under the
project name at the top of console.cloud.google.com (for example
`my-first-project-12345`).

**Website / home page:** https://cljxs.github.io/cljxs/
**Privacy policy:** https://cljxs.github.io/cljxs/privacy.html
**Terms of service:** https://cljxs.github.io/cljxs/terms.html

**Describe your use case** (paste as is):

> Clip is a personal tool I run for my own YouTube channel. It takes long
> videos I have the rights to use (my own recordings, footage from creators
> who have given me permission through clipping campaigns, or openly
> licensed works) and cuts them into short vertical clips with captions.
> I review every clip and approve it myself before it is uploaded. The API
> is used for two things: videos.insert, to upload a clip I have approved
> to my own channel with the title, description and tags I reviewed; and
> read-only public data (videos.list for most-popular charts, channels.list
> and search.list), to see which creators and moments are trending, so I
> know which parts of my permitted footage to clip. Clip does not download
> YouTube content, does not act on other users' accounts, and stores only
> my own sign-in token, on my private server.

**Expected volume:** up to about 6 uploads a day. Research uses about
1,000 of the 10,000 daily units. No quota increase is needed; the request
is to lift the private-only restriction on uploads.

**Who uses it:** only me, the channel owner. There are no other users.
