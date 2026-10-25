# Revised announcement

Posted to r/AcuRite:
https://www.reddit.com/r/AcuRite/comments/1x7qvxj/revised_announcement_acurite_free_open_source/

It links back to the original post, [README.md](README.md).

---

I made a web based replacement for all AcuRite radio-to-internet weather stations app and web page. It runs on a Pi400. WRT this announcement, I hope this is a more enjoyable read than the last one! This writing is designed so you can read until you're done and ignore the rest.

Why you might have any interest at all in this post: If you have an AcuRite weather station, you may find this interesting or entertaining. Else you might wanna give it a pass. Or not. Up to you.

If you want to get a copy or know more, here's the original post: https://github.com/akienm/CC0AC-Weather/blob/main/reddit_announcement/README.md

Why this is here?: Because I wrote something for myself and somebody convinced me I should post it to relevant Reddit groups. I had Claude generate a post and 4 folks seemed only to wanna pick at details. One got snotty and blocked me when I didn't put up with that. So sad. Anyway... This specific post is here because dropping a post about the software seemed to stir up hornets. Even tho these 3 reddit communities are new to me, doesn't mean I shouldn't introduce myself. And I really didn't before. Legit apologies.

Cut to the chase, dude, I don't have all day: I wrote a web based replacement for the AcuRite software. Free, open source software. Designed such that if the community wanted to make it deal with more kinds of devices, or change how it worked or looked or whatever... The code is structured so y'all could do that. So if you wanna replace your AcuRite software and are willing to run it on your own computer, this MIGHT be for you. As I write this, it should work with any AcuRite gear that can tie to an AcuRite Access network radio (Atlas, Iris, etc). I will be adding support for RTL-SDR Blog V4 when that arrives, which should make it possible to work with a lot more weather gear. Does not REQUIRE a pi, but that does make setup easier.

To be clear: there are several alternatives, two of the most noteworthy are AcuParse (better for me as a DIYer, but less good for my wife) and Zazder (good for folks with the Apple fetish, and I think I heard an Android version is in the works) as I believe they both also let you store your data yourself, which means no AcuRite needed (same as mine)... But there are many many other weather and home automation tools.

The longer story: I have an AcuRite weather system that I've had since 2014. In that time, AcuRite obsolized my AcuRite Hub and if I wanted to keep my gear, I had to buy a new AcuRite Access. So I did. Then a few months ago I started seeing the alert in my app that the web page and app would be going away. And they'd really like it if I'd pony up for a subscription!

So I took to reddit briefly, and found many folks complaining about the AcuRite decision, AND THE APPARENT LACK OF OTHER OPTIONS. I also wrote AcuRite about this, and yeah, they're removing the existing web page and app. Apparently they don't make money on hosting for me, which they were, and so they want to recover some of those costs. While I can understand that, I didn't buy their gear to be taxed ongoingly. So I told them I'd produce a free, open source solution and to hell with them.

I explained to Claude Code what I wanted... To look as close as possible to the existing page without trampling anybody's trademarks or copyrights. Then I asked it if there were any 3rd party tools already in existence that already did this? Claude looked at what I wanted and said no. Now to be clear, I interpreted this to mean that external options didn't exist. Claude on the other hand said there were no tools with dashboards that replaced this one out of the box. A different thing... And as I said, it turns out there were more tools than I knew of when I started my work.

So I described exactly what I wanted from Claude and we went through about a week's worth of work. Making sure there were unit tests for every piece, that there were templates so folks could easily modify it, that the data collection from the radio was one component separate from everything else so it could be swapped out, same with the interpretation of the data (to allow more models or manufacturers in the future), same with the recording and UI. What it doesn't yet have: security. And right now it's only linux. Mine runs on a raspberry pi because that way I can update the other computers without bringing down my weather station. We even built an installer that sets up a pi for you with everything all set to go. It asks the user credentials you want for the new system and away it goes. This lets us plug the acurite radio into the pi directly, and not have to fuss with reconfiguring everything. If you want it available away from home, then you'll have to look up how your router does port forwarding. I think we default to port 12345.

To be clear: I wrote the requirements, Claude wrote all the code, and I did all the QA, unit and integration testing, and other validation. All straight python, no frameworks.

So who the hell are you anyway?: Blue collar family, dad worked for the phone company. I started playing with electronics in 1975, and with computers professionally in 1982. Today I'm an Enterprise Test Automation Architect for the Delta Dental family of companies. Part of a team of 4, our job is to train and mentor about 100 test automation engineers across all the companies. Including making sure folks know how to use AI well, with good QA practices and not wasting tokens.

I am also part of a collection of folks who have helped enact a moratorium in Grant County New Mexico against the data center that wanted to move in. Not because I think everybody should be against datacenters, that's the very worst kind of NIMBY, and I have no respect for that. Our plan is to not treat data centers as something we buy via incentives like they want, but instead... If they're going to build in our community, they have to become part of the community. That doesn't mean the 4 jobs most of these places will bring... If that... It means if you want water, you have to help improve the water situation here. You want power, you have to bring in more or diverse power sources to the area. Build to keep the noise down for your neighbors, including the wildlife. You want access? help fix up the roads. Or parks. Or whatever. That the cost of building that here be that having it here benefits everybody.

Finally, my side project outside of work isn't to build weather gear. That was a one off because I'm big on right to repair. No my side project is about the fact that AI uses GPUs to calculate every possible answer to the query given them, and then throw away everything but the right one. That's why they are so power hungry. I want to instead use graph trees to build an AI that only computes the correct path and so uses far less power. And yes, I have a plan for that and know about the issues with graph trees. Won't be quick work. But if I'm successful, AI stops being a thing only billionaires own. AI for everybody on cheap hardware. Like your old laptop. Or phone. I think it perfectly reasonable to use current AI to build it's way more efficient successor.

But you claimed you wrote it, when Claude did! This is becoming the new grammer-nazi saying. Yeah, I say it like that because in the real world of the workplace, that's how people are talking about it. I wrote it even if Claude did the bulk of the coding. Because I am still the one responsible. I have to design it, I have to spoon feed each requirements choice to Claude and make sure it's understood. Especially since it's to be used and possibly improved by others, it has to be structured with care. I have to validate it. I'm the one who catches trouble if it's wrong. We don't get to just poof things into place and hope for the best.

You don't like that phrasing, you do you, but I won't be listening since a) that's not what the workplace looks like and b> that's just about all you're bringing to the table with that comment.

And I'm also an ADD boy of many years and AI makes my life so I feel like I can actually get a day's worth of work out of myself without getting too distracted. It's a tool, but it's still years of experience driving my work.

And to be clear: Yes, I think AI is causing us to lose our minds a little at a time. Many programmers are forgetting how to program. And then they run into trouble and have to learn quickly. But that kind of behavior isn't new either. Look what the modern internet has done to writing skills and civility.
