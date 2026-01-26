// Services/BotService.cs
using Telegram.Bot;
using Telegram.Bot.Types;

namespace ForchilacraftTradeBot.Services;

public class BotService : BackgroundService
{
    private readonly ILogger<BotService> _logger;
    
    public BotService(ILogger<BotService> logger)
    {
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        _logger.LogInformation("Bot service started");
        
        while (!stoppingToken.IsCancellationRequested)
        {
            await Task.Delay(1000, stoppingToken);
        }
    }
}